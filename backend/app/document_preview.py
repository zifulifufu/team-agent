"""Render document pages for group members to inspect with review_picture.

The renderer reports files and page coverage, never a visual-quality verdict.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from . import assemble, bindirs
from .coderun import kill_group


def binary(name: str) -> str:
    override = os.environ.get("TEAM_AGENT_" + name.upper(), "")
    bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override" / name
    for candidate in (override, bindirs.tool(name), str(bundled)):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


def available() -> bool:
    return bool(binary("soffice") and binary("pdftoppm"))


async def _run(cmd: list[str], env: dict) -> None:
    proc = await asyncio.create_subprocess_exec(*cmd, env=env, start_new_session=True,
                                               stdout=asyncio.subprocess.PIPE,
                                               stderr=asyncio.subprocess.STDOUT)
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), 120)
        if proc.returncode:
            raise ValueError((output or b"").decode("utf-8", "replace")[-500:])
    finally:
        kill_group(proc.pid)
        await proc.wait()


async def render(workspace: Path, path: str, start_page: int = 1, max_pages: int = 12) -> dict:
    from pypdf import PdfReader

    root = workspace.resolve()
    source = assemble._local(root, path, "Give a DOCX or PDF path / 请提供 DOCX 或 PDF 路径")
    if source.suffix.lower() not in {".docx", ".pdf"} or not 0 < source.stat().st_size <= 20_000_000:
        raise ValueError("Use a nonempty DOCX/PDF under 20 MB / 请使用小于20MB的非空DOCX或PDF")
    if start_page < 1 or not 1 <= max_pages <= 12:
        raise ValueError("start_page >= 1; max_pages must be 1–12")
    if not available():
        raise ValueError("Document preview needs soffice and pdftoppm / 文档预览缺少soffice或pdftoppm")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    base = root / "document-previews"
    if base.is_symlink():
        raise ValueError("Preview directory must not be a symlink")
    base.mkdir(exist_ok=True)
    env = dict(os.environ)
    # The bundled headless runtime needs its fontconfig to locate macOS CJK fonts.
    fonts = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/libreoffice-headless/libreoffice/LibreOfficeDev.app/Contents/Resources/fontconfig/fonts.conf"
    if fonts.is_file():
        env.setdefault("FONTCONFIG_FILE", str(fonts))
    with tempfile.TemporaryDirectory(prefix="render-", dir=base) as tmp:
        folder = Path(tmp)
        pdf = folder / "source.pdf"
        if source.suffix.lower() == ".pdf":
            shutil.copyfile(source, pdf)
        else:
            # A private profile prevents locking or changing an open desktop office session.
            profile = folder / "profile"
            settings = profile / "user"
            settings.mkdir(parents=True)
            (settings / "registrymodifications.xcu").write_text(
                '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
                '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
                '</item></oor:items>')
            await _run([binary("soffice"), "-env:UserInstallation=" + profile.as_uri(),
                        "--headless", "--norestore", "--convert-to", "pdf", "--outdir", str(folder), str(source)], env)
            converted = folder / (source.stem + ".pdf")
            if not converted.is_file():
                raise ValueError("No PDF produced / 未生成PDF")
            converted.rename(pdf)
        pages = len(PdfReader(pdf).pages)
        if not start_page <= pages:
            raise ValueError(f"Document has {pages} pages; start_page is outside it")
        end = min(pages, start_page + max_pages - 1)
        await _run([binary("pdftoppm"), "-png", "-r", "110", "-f", str(start_page), "-l", str(end),
                    str(pdf), str(folder / "page")], env)
        pngs = sorted(folder.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[-1]))
        if len(pngs) != end - start_page + 1:
            raise ValueError("Page rendering incomplete / 页面渲染不完整")
        # A unique output per call prevents stale previews or symlink overwrites.
        output = Path(tempfile.mkdtemp(prefix=digest[:12] + "-", dir=base))
        files = []
        for rendered in [pdf, *pngs]:
            target = output / rendered.name
            shutil.move(rendered, target)
            files.append({"name": str(target.relative_to(root)), "kind": "image" if target.suffix == ".png" else "file",
                          "bytes": target.stat().st_size})
    return {"source": path, "sha256": digest, "page_count": pages,
            "rendered_pages": [start_page, end], "files": files}
