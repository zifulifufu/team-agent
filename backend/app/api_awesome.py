"""示例库(融合 awesome-llm-apps):浏览、导入、从本地克隆刷新。

  * 只读静态数据:程序附带一份快照(app/data/awesome_apps.json + 中文说明 awesome_zh.json),
    用户可以指向自己 clone 的仓库「刷新」——刷新只用 ast 读源码,从不 import、不运行仓库里的任何代码;
  * 导入的东西都遵守本程序的老规矩:MCP 服务器导入后一律是「停用」状态(它是会在本机运行的命令,要你自己核对后再启用),
    不会替你填任何密钥;成员/提示词/技能只是文字。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import awesome_apps as aa
from .store import Store
from .templates import ensure_agent
from .tools import list_skills, safe_skill_name, write_skill

ZH_PATH = Path(__file__).parent / "data" / "awesome_zh.json"
PLACEHOLDER_DIR = "/path/to/allowed/dir"
SKILL_NOTE = "> 说明:这是 awesome-llm-apps 里的技能。它原本配合脚本或命令行使用;群里的成员没有那些程序,这里只能把它当作方法和流程的参考。\n\n"


class CreateGroupIn(BaseModel):
    name: str | None = None


class MembersIn(BaseModel):
    group_id: str | None = None
    names: list[str] | None = None       # 只要其中几个;空 = 全部


class PromptsIn(BaseModel):
    names: list[str] | None = None


class RefreshIn(BaseModel):
    path: str


def _zh() -> dict:
    try:
        d = json.loads(ZH_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def build_awesome_router(store: Store) -> APIRouter:
    r = APIRouter()
    zh = _zh()

    def data() -> tuple[dict, str]:
        return aa.load(store.data_dir)

    def find_app(app_id: str) -> tuple[dict, dict]:
        d, _ = data()
        a = next((x for x in d["teams"] + d["agents"] if x["id"] == app_id), None)
        if not a:
            raise HTTPException(404, "示例库里没有这一项(快照可能已被刷新)")
        return a, d

    def app_texts(a: dict) -> tuple[str, str]:
        z = (zh.get("apps") or {}).get(a["path"]) or {}
        return z.get("title") or a["title"], z.get("desc") or _clip(a.get("desc", ""), 200)

    def unique_agent_name(base: str, taken: set[str]) -> str:
        name, n = base, 2
        while name in taken:
            suffix = f"_{n}"
            name = base[: 30 - len(suffix)] + suffix
            n += 1
        taken.add(name)
        return name

    def make_members(a: dict, names: list[str] | None) -> list[dict]:
        picks = [x for x in a["agents"] if not names or x["name"] in names]
        taken = {x["name"] for x in store.list_agents()}
        out = []
        for x in picks:
            m = aa.member_from(x, a["title"])
            out.append(store.create_agent(unique_agent_name(m["name"], taken), m["avatar"], m["role"], m["prompt"],
                                          None, [], m["tags"]))
        return out

    # ---------------------------------------------------------------- 浏览
    @r.get("/api/awesome")
    async def overview() -> dict:
        d, origin = data()
        have_skill = {s.name for s in list_skills(store.data_dir / "skills")}
        have_mcp = {m["name"] for m in store.list_mcp()}
        zs, zm = zh.get("skills") or {}, zh.get("mcp") or {}

        def app_row(a: dict) -> dict:
            title, desc = app_texts(a)
            lead = a.get("lead")
            return {
                "id": a["id"], "title": title, "title_en": a["title"], "desc": desc, "category": a.get("category", ""),
                "framework": a.get("framework", ""), "path": a["path"], "kind": a["kind"],
                "members": [{"name": x["name"], "role": _clip(x.get("role") or x.get("description") or "", 80),
                             "tools": x.get("tools") or []} for x in a["agents"]],
                "lead": lead.get("name") if isinstance(lead, dict) else None,
                "sequential": bool(a.get("sequential")),
            }

        return {
            "source": d["source"], "commit": d.get("commit", ""), "generated_at": d.get("generated_at", ""), "origin": origin,
            "teams": [app_row(a) for a in d["teams"]],
            "agents": [app_row(a) for a in d["agents"]],
            "skills": [{
                "id": s["id"], "name": s["name"], "title": (zs.get(s["name"]) or {}).get("title", s["name"]),
                "desc": (zs.get(s["name"]) or {}).get("desc") or _clip(s.get("description", ""), 200),
                "needs_runtime": bool(s.get("needs_runtime")), "clipped": bool(s.get("clipped")),
                "compatibility": _clip(s.get("compatibility", ""), 160), "installed": safe_skill_name(s["name"]) in have_skill,
            } for s in d["skills"]],
            "mcp": [{
                "id": m["id"], "name": m["name"], "command": m["command"], "args": m["args"], "env_keys": m.get("env_keys", []),
                "note": zm.get(m["name"]) or m.get("note", ""), "installed": m["name"] in have_mcp,
            } for m in d["mcp"]],
        }

    # ---------------------------------------------------------------- 团队 → 群聊
    @r.post("/api/awesome/apps/{app_id}/create-group")
    async def create_group(app_id: str, body: CreateGroupIn) -> dict:
        a, d = find_app(app_id)
        title, _ = app_texts(a)
        members = make_members(a, None)
        if not members:
            raise HTTPException(400, "这个示例里没有提取到可用的角色")
        lead = a.get("lead") if isinstance(a.get("lead"), dict) else None
        if lead and lead.get("name"):
            hm = aa.member_from({**lead, "role": "Team lead"}, a["title"])
            taken = {x["name"] for x in store.list_agents()}
            host = store.create_agent(unique_agent_name(hm["name"], taken), "🧭", "群主 · 统筹", hm["prompt"], None, [], ["推理"])
            members.insert(0, host)
        else:
            host = ensure_agent(store, "小助") or members[0]
            if host["id"] not in [m["id"] for m in members]:
                members.insert(0, host)
        flow = ""
        if a.get("sequential") and len(members) > 1:
            flow = "本群成员按流水线顺序接力,上一位的产出交给下一位:" + " → ".join(m["name"] for m in members if m["id"] != host["id"]) + "。\n"
        prompt = (f"本群来自开源示例「{a['title']}」({d['source']['name']},{d['source']['license']}):{_clip(a.get('desc', ''), 240)}\n"
                  f"{flow}成员的角色说明取自原示例;原示例给它们配的联网、抓取等工具在这里不一定有,做不到时请直接说明,不要假装完成。")
        g = store.create_group((body.name or title).strip()[:60] or title, host["id"], [m["id"] for m in members],
                               ext={"skills": []}, prompt=prompt)
        return {"group": g, "members": [m["name"] for m in members], "host": host["name"]}

    # ---------------------------------------------------------------- 单个角色 → 成员
    @r.post("/api/awesome/apps/{app_id}/members")
    async def add_members(app_id: str, body: MembersIn) -> dict:
        a, _ = find_app(app_id)
        if body.group_id and not store.get_group(body.group_id):
            raise HTTPException(404, "群聊不存在")
        made = make_members(a, body.names)
        if not made:
            raise HTTPException(400, "没有匹配的角色")
        if body.group_id:
            for m in made:
                store.add_member(body.group_id, m["id"])
        return {"members": [{"id": m["id"], "name": m["name"]} for m in made]}

    # ---------------------------------------------------------------- 角色提示词 → 提示词库
    @r.post("/api/awesome/apps/{app_id}/prompts")
    async def add_prompts(app_id: str, body: PromptsIn) -> dict:
        a, _ = find_app(app_id)
        have = {p["title"] for p in store.list_prompts()}
        added, skipped = [], []
        for x in a["agents"]:
            if body.names and x["name"] not in body.names:
                continue
            text = (x.get("instructions") or x.get("description") or "").strip()
            if not text:
                continue
            title = f"[示例] {_clip(a['title'], 40)} · {_clip(x['name'], 30)}"
            if title in have:
                skipped.append(title)
                continue
            store.add_prompt(title, text + "\n\n(来源:awesome-llm-apps,Apache-2.0)", "general", False)
            have.add(title)
            added.append(title)
        return {"added": added, "skipped": skipped}

    # ---------------------------------------------------------------- 技能
    @r.post("/api/awesome/skills/{sid}/install")
    async def install_skill(sid: str) -> dict:
        d, _ = data()
        s = next((x for x in d["skills"] if x["id"] == sid), None)
        if not s:
            raise HTTPException(404, "示例库里没有这个技能")
        name = safe_skill_name(s["name"])
        if not name:
            raise HTTPException(400, "技能名称不合法")
        if any(x.name == name for x in list_skills(store.data_dir / "skills")):
            raise HTTPException(409, "已有同名技能")
        body = (SKILL_NOTE if s.get("needs_runtime") else "") + s["body"]
        if s.get("clipped"):
            body += "\n\n(为控制长度,原技能正文在这里被截短了,完整版见 awesome-llm-apps 仓库。)"
        desc = (((zh.get("skills") or {}).get(s["name"]) or {}).get("desc")) or _clip(s.get("description", ""), 200)
        sk = write_skill(store.data_dir / "skills", name, desc, body, "member", version=str(s.get("version") or ""))
        return {"name": sk.name}

    # ---------------------------------------------------------------- MCP:只加进列表,一律停用
    @r.post("/api/awesome/mcp/{mid}/add")
    async def add_mcp(mid: str) -> dict:
        d, _ = data()
        m = next((x for x in d["mcp"] if x["id"] == mid), None)
        if not m:
            raise HTTPException(404, "示例库里没有这个 MCP")
        if any(x["name"] == m["name"] for x in store.list_mcp()):
            raise HTTPException(409, "已有同名 MCP 服务器")
        args = [PLACEHOLDER_DIR if a == "/tmp" else a for a in m["args"]]
        note = ((zh.get("mcp") or {}).get(m["name"])) or m.get("note", "")
        row = store.add_mcp(m["name"], m["command"], args, {k: "" for k in m.get("env_keys", [])}, "", "", {},
                            (note + " 来自 awesome-llm-apps;导入后是停用状态,核对命令、填好密钥后再启用。").strip())
        store.update_mcp(row["id"], {"enabled": False})
        return {"id": row["id"], "name": row["name"]}

    # ---------------------------------------------------------------- 从本地克隆刷新
    @r.post("/api/awesome/refresh")
    async def refresh(body: RefreshIn) -> dict:
        root = Path(body.path.strip()).expanduser()
        try:
            new = await asyncio.to_thread(aa.extract, root)      # 只读文件、ast 解析;不运行仓库里的任何代码
        except aa.AwesomeError as e:
            raise HTTPException(400, str(e)) from None
        except OSError as e:
            raise HTTPException(400, f"读取失败:{e}") from None
        if aa.validate(new) or not (new["teams"] or new["agents"] or new["skills"] or new["mcp"]):
            raise HTTPException(400, "没有从这个目录里提取到任何内容,已保留原来的快照")
        tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=store.data_dir, suffix=".tmp", delete=False)
        try:
            json.dump(new, tmp, ensure_ascii=False)
            tmp.close()
            os.replace(tmp.name, store.data_dir / "awesome_apps.json")
        finally:
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)
        return {"commit": new.get("commit", ""), "teams": len(new["teams"]), "agents": len(new["agents"]),
                "skills": len(new["skills"]), "mcp": len(new["mcp"])}

    @r.post("/api/awesome/reset")
    async def reset() -> dict:
        p = store.data_dir / "awesome_apps.json"
        if p.exists():
            p.unlink()
        return {"ok": True}

    return r
