"""本地模型推荐目录 + 本机硬件检测 + 「这台电脑跑得动吗」的粗略评估。

目录随程序附带一份快照(app/data/local_models.json),数据目录里的同名文件(由「更新与发现」从 GitHub
拉取)如果 version 更大就覆盖它;用户在界面里「加入推荐」的新型号另存在 local_extras.json。
大小取自 ollama.com/library 的标签页,评估是经验估算,不是保证:
- 模型权重要整个放进内存(或显存),所以拿「模型大小」和「本机内存」比;
- 没有 GPU 加速(Intel Mac、没有 NVIDIA 卡的电脑)时,能装下不等于跑得快,大模型会非常慢。
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import i18n, strengths
from .versions import is_newer

SHIPPED = Path(__file__).parent / "data" / "local_models.json"

TAG_RE = re.compile(r"^(?:[a-z0-9][a-z0-9._-]{0,60}/)?[a-z0-9][a-z0-9._-]{0,60}(?::[A-Za-z0-9][A-Za-z0-9._-]{0,80})?$")
# 不是对话模型的库(嵌入、安全审核、OCR、重排序……),不要当作「新的大语言模型」推荐
NON_CHAT_RE = re.compile(r"embed|guard|safeguard|shield|ocr|rerank|bge-|minilm|reader-lm|nuextract", re.I)


def valid_tag(tag: str) -> bool:
    return bool(isinstance(tag, str) and TAG_RE.match(tag))


def base_name(tag: str) -> str:
    return tag.split(":", 1)[0]


# ------------------------------------------------------------------ 硬件
def _ram_gb() -> float | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
    except (ValueError, OSError, AttributeError):
        return None


def _sysctl(name: str) -> str:
    try:
        r = subprocess.run(["/usr/sbin/sysctl", "-n", name], capture_output=True, text=True, timeout=2)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def native_machine(system: str, machine: str) -> tuple[str, bool]:
    """(真实的 CPU 架构, 当前 Python 是不是被 Rosetta 转译着跑的)。
    苹果芯片上装了 x86_64 版的 Python 时,platform.machine() 会说 x86_64,但硬件其实是 arm64、有 Metal 加速;
    不纠正的话,所有大模型都会被误判成「会很慢」。"""
    if system == "Darwin" and machine.lower() in ("x86_64", "amd64"):
        if _sysctl("sysctl.proc_translated") == "1" or _sysctl("hw.optional.arm64") == "1":
            return "arm64", True
    return machine, False


def _accel(system: str, machine: str) -> str:
    """推测有没有 GPU 加速:metal(Apple 芯片)/ cuda(检测到 nvidia-smi)/ none / unknown。"""
    m = machine.lower()
    if system == "Darwin":
        return "metal" if m in ("arm64", "aarch64") else "none"
    if shutil.which("nvidia-smi"):
        return "cuda"
    return "none" if system == "Linux" else "unknown"


def hardware(path: str | None = None) -> dict[str, Any]:
    """本机(运行后端的这台电脑)的内存、磁盘、加速方式。"""
    system = platform.system()
    machine, translated = native_machine(system, platform.machine())
    ram = _ram_gb()
    target = path or os.path.expanduser("~")
    try:
        free = shutil.disk_usage(target).free / 1024**3
    except OSError:
        free = None
    return {
        "system": system,
        "machine": machine,
        "translated": translated,
        "ram_gb": round(ram, 1) if ram is not None else None,
        "disk_free_gb": round(free, 1) if free is not None else None,
        "accel": _accel(system, machine),
    }


def assess(size_gb: float, hw: dict[str, Any]) -> dict[str, Any]:
    """fit: ok(内存宽裕)/ tight(能装下但很紧)/ no(装不下)/ unknown;
    disk_ok: 磁盘放得下;slow: 没有 GPU 加速且模型较大,会很慢。"""
    ram, free = hw.get("ram_gb"), hw.get("disk_free_gb")
    if not size_gb:
        return {"fit": "unknown", "disk_ok": True, "slow": False}
    if ram is None:
        fit = "unknown"
    elif size_gb <= ram * 0.5:
        fit = "ok"
    elif size_gb <= ram * 0.8:
        fit = "tight"
    else:
        fit = "no"
    disk_ok = True if free is None else size_gb * 1.05 <= free
    slow = hw.get("accel") in ("none",) and size_gb >= 9
    return {"fit": fit, "disk_ok": disk_ok, "slow": slow}


# ------------------------------------------------------------------ 目录数据
def validate(data: object) -> str | None:
    """返回错误说明;合法则返回 None。用于校验从网络下载的目录(不可信输入)。"""
    if not isinstance(data, dict) or not isinstance(data.get("version"), str) or not data["version"]:
        return "缺少 version"
    fams = data.get("families")
    if not isinstance(fams, list) or not fams:
        return "缺少 families"
    for f in fams:
        if not isinstance(f, dict) or not isinstance(f.get("id"), str) or not isinstance(f.get("models"), list):
            return "型号系列格式不对"
        for m in f["models"]:
            if not isinstance(m, dict) or not valid_tag(m.get("tag", "")):
                return f"{f.get('id')}: 型号标签不合法"
            if not isinstance(m.get("size_gb"), (int, float)) or isinstance(m.get("size_gb"), bool) or m["size_gb"] < 0:
                return f"{m['tag']}: size_gb 不合法"
    for key in ("selfhost", "cloud_only"):
        if key in data and not isinstance(data[key], list):
            return f"{key} 格式不对"
    return None


def _load(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if validate(d) is None else None
    except (OSError, ValueError):
        return None


class LocalCatalog:
    def __init__(self, data_dir: Path | str):
        self.override_path = Path(data_dir) / "local_models.json"
        self.extras_path = Path(data_dir) / "local_extras.json"
        self.data: dict = {}
        self.source = "shipped"
        self.reload()

    def reload(self) -> None:
        base = _load(SHIPPED) or {"version": "", "families": []}
        over = _load(self.override_path)
        if over and is_newer(over["version"], base.get("version", "")):
            self.data, self.source = over, "override"
        else:
            self.data, self.source = base, "shipped"

    @property
    def version(self) -> str:
        return str(self.data.get("version", ""))

    def save_override(self, data: dict) -> None:
        self.override_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.override_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.override_path)
        self.reload()

    # ---- 用户/发现流程加入的型号
    def extras(self) -> list[dict]:
        try:
            d = json.loads(self.extras_path.read_text(encoding="utf-8"))
            return [e for e in d if isinstance(e, dict) and valid_tag(e.get("tag", ""))] if isinstance(d, list) else []
        except (OSError, ValueError):
            return []

    def add_extra(self, tag: str, size_gb: float, note: str = "", source: str = "manual") -> dict:
        if not valid_tag(tag):
            raise ValueError("型号标签不合法")
        items = [e for e in self.extras() if e["tag"] != tag]
        entry = {"tag": tag, "size_gb": round(float(size_gb), 1), "note": note[:200], "source": source, "added_at": time.time()}
        items.append(entry)
        self.extras_path.parent.mkdir(parents=True, exist_ok=True)
        self.extras_path.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        return entry

    def remove_extra(self, tag: str) -> bool:
        items = self.extras()
        keep = [e for e in items if e["tag"] != tag]
        if len(keep) == len(items):
            return False
        self.extras_path.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
        return True

    # ---- 已知型号(避免把同一个新型号反复当成「新发现」)
    def known_bases(self) -> set[str]:
        out: set[str] = set()
        for f in self.data.get("families", []):
            out.update(f.get("track", []))
            out.update(base_name(m["tag"]) for m in f["models"])
        out.update(base_name(e["tag"]) for e in self.extras())
        out.update(c["name"] for c in self.data.get("cloud_only", []) if isinstance(c, dict) and c.get("name"))
        return out

    def tracked_bases(self) -> list[str]:
        """需要探测「有没有更新版本」的型号名(每个系列里最新的那几代)。"""
        out: list[str] = []
        for f in self.data.get("families", []):
            for n in f.get("track", []):
                if n not in out:
                    out.append(n)
        return out

    def watch(self) -> dict[str, list[str]]:
        w = self.data.get("watch") or {}
        return {k: [x for x in w.get(k, []) if isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", x)]
                for k in ("github_orgs", "hf_authors")}

    # ---- 给界面的完整视图
    def view(self, installed: set[str] | None = None, hw: dict[str, Any] | None = None) -> dict[str, Any]:
        lang = i18n.current()
        data = i18n.localize(self.data, lang)      # English by default, Chinese for the zh UI
        hw = hw or hardware()
        have = installed or set()

        def is_installed(tag: str) -> bool:
            return tag in have or f"{tag}:latest" in have or (":" not in tag and f"{tag}:latest" in have)

        def row(m: dict) -> dict:
            return {**m, **assess(float(m["size_gb"]), hw), "installed": is_installed(m["tag"])}

        fams = []
        for f in data.get("families", []):
            fams.append({
                "id": f["id"], "vendor": f.get("vendor", ""), "name": f.get("name", f["id"]), "desc": f.get("desc", ""),
                "license": f.get("license", ""), "strengths": strengths.clean_tags(f.get("strengths", [])),
                "models": [row(m) for m in f["models"]],
            })
        ex = self.extras()
        if ex:
            fams.append({
                "id": "extras",
                "vendor": i18n.pick(lang, "Added by me", "我加入的"),
                "name": i18n.pick(lang, "Newly found / added by hand", "新发现 / 手动加入"),
                "extra": True,
                "desc": i18n.pick(
                    lang,
                    "Models added from Updates & discovery or by hand; sizes come from the Ollama library listing.",
                    "从「更新与发现」或手动加入的新型号,大小来自 Ollama 库的清单。",
                ),
                "license": "", "strengths": [],
                "models": [row({"tag": e["tag"], "size_gb": e["size_gb"], "ctx": "", "note": e.get("note", "")}) for e in ex],
            })
        selfhost = []
        for s in data.get("selfhost", []):
            selfhost.append({
                "id": s.get("id", ""), "vendor": s.get("vendor", ""), "name": s.get("name", ""), "desc": s.get("desc", ""),
                "license": s.get("license", ""), "strengths": strengths.clean_tags(s.get("strengths", [])),
                "models": [{**m, **assess(float(m.get("size_gb") or 0), hw)} for m in s.get("models", [])],
            })
        return {
            "version": self.version, "source": self.source, "note": data.get("note", ""), "hardware": hw,
            "families": fams, "selfhost": selfhost, "cloud_only": data.get("cloud_only", []),
        }


# ------------------------------------------------------------------ 发现流程用的纯函数
def parse_library_names(html: str) -> list[str]:
    """从 ollama.com/library 页面里按出现顺序取型号名(去重)。页面结构变了就会取不到,调用方要能容忍。"""
    seen: list[str] = []
    for n in re.findall(r'href="/library/([a-z0-9][a-z0-9._-]*)"', html):
        if n not in seen:
            seen.append(n)
    return seen


def successor_names(name: str) -> list[str]:
    """猜「下一代」的型号名:qwen3.8 → qwen3.9、qwen4;glm-4.7-flash → glm-4.8-flash、glm-5-flash。
    只是猜测,必须再用 Ollama 注册表验证这个名字真的存在。"""
    m = re.search(r"(\d+)(?:\.(\d+))?", name)
    if not m:
        return []
    pre, post = name[: m.start()], name[m.end():]
    major, minor = int(m.group(1)), m.group(2)
    out = []
    if minor is not None:
        out.append(f"{pre}{major}.{int(minor) + 1}{post}")
    else:
        out.append(f"{pre}{major}.1{post}")
    out.append(f"{pre}{major + 1}{post}")
    return [n for n in dict.fromkeys(out) if n != name]


def manifest_size_gb(manifest: Any) -> float | None:
    """Ollama 注册表清单(Docker manifest 格式)里各层大小之和,单位 GB。"""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("layers"), list):
        return None
    total = sum(int(layer.get("size", 0)) for layer in manifest["layers"] if isinstance(layer, dict))
    return round(total / 1024**3, 1) if total > 0 else None
