"""模板中心接口:目录 / 详情 / 一键应用。

  * `GET  /api/gallery`            —— 目录(分类、条目、已装状态、自定义模板的加载情况)
  * `GET  /api/gallery/{id}`       —— 单条详情(含完整正文,供安装前阅读)
  * `POST /api/gallery/{id}/apply` —— 一键应用(建群 / 建成员 / 装技能 / 存提示词 / 加 MCP)

应用是幂等的:重复点不会产生重复的技能、提示词或 MCP;成员按名字复用;
建群则每次新建一个(同名自动加序号)。全部只往本机数据库与 skills 目录写文本。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import gallery
from .store import Store


class ApplyIn(BaseModel):
    name: str | None = None          # team:建群用的名字(默认取模板名,重名自动加序号)
    group_id: str | None = None      # agent:顺带拉进哪个群
    overwrite: bool = False          # skill / prompt:已存在时是否覆盖为模板里的版本


def build_gallery_router(store: Store) -> APIRouter:
    r = APIRouter()

    @r.get("/api/gallery")
    async def overview() -> dict:
        return gallery.overview(store)

    @r.get("/api/gallery/{item_id}")
    async def detail(item_id: str) -> dict:
        it = gallery.find(store, item_id)
        if it is None:
            raise HTTPException(404, "模板不存在")
        return it

    @r.post("/api/gallery/{item_id}/apply")
    async def apply_template(item_id: str, body: ApplyIn) -> dict:
        try:
            return gallery.apply(store, item_id, body.model_dump())
        except gallery.GalleryError as e:
            raise HTTPException(400, str(e)) from None

    return r
