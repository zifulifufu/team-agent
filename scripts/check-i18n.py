#!/usr/bin/env python3
"""Check the front end's i18n: no English leaking into a Chinese build.

Two things are checked, because they fail differently:

  1. Keys used by `t()` / `tr()` with no entry in the ZH dictionary. The user sees the English key.
  2. `tr()` evaluated while a module loads — a top-level `const X = ... tr(...)`. Its result is
     computed once, at import time, and never again: the page then shows whatever language happened
     to be current then, and ignores every later language switch. Component code must use
     `useI18n().t`, and non-component helpers may keep `tr()` *inside a function body*, which runs
     when it is called.

`pick(en, zh)` is deliberately ignored: it carries its own Chinese, so it needs no dictionary entry.

Both sides are unescaped before comparison — a dictionary key written `"Say \"hi\""` and a call
written `'Say "hi"'` are the same string at runtime, and a naive comparison reports a false gap.
Comments are skipped too, for the same reason: the dictionary's own documentation contains
`t("...")` examples.

Usage: check-i18n.py [src-dir] [i18n-file]      (defaults: desktop/src, desktop/src/i18n.tsx)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CALL = re.compile(r"""\b(?:t|tr)\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""")
ENTRY = re.compile(r"""^\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*:""", re.M)
# Module-level only: a top-level binding starts in column 0. A `const` inside a function body is
# indented, and its tr() runs when the function is called — which is exactly what non-component
# helpers are supposed to do.
BINDING = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*(?::[^=]+)?=")
# A field of a module-level object/array literal (two-space indent), e.g. `  auto: tr("..."),`
# or `  { id: "x", label: tr("...") },`
FIELD = re.compile(r"^ {2}(?:[A-Za-z_$][\w$]*\s*:\s*tr\(|\{[^\n]*:\s*tr\()")

ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "0": "\0",
           '"': '"', "'": "'", "`": "`", "\\": "\\", "/": "/"}

HERE = Path(__file__).resolve().parent


def unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: ESCAPES.get(m.group(1), m.group(1)), s)


def without_comments(text: str) -> str:
    out, in_block = [], False
    for line in text.splitlines():
        stripped = line.lstrip()
        if in_block:
            out.append("")
            if "*/" in line:
                in_block = False
            continue
        if stripped.startswith("/*"):
            out.append("")
            in_block = "*/" not in line
            continue
        if stripped.startswith(("//", "*")):
            out.append("")
            continue
        out.append(line)
    return "\n".join(out)


def dictionary_keys(text: str) -> set[str]:
    start = text.index("const ZH")
    end = text.index("\n};", start)
    return {unescape(a or b) for a, b in ENTRY.findall(text[start:end])}


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "desktop" / "src"
    i18n = Path(sys.argv[2]) if len(sys.argv) > 2 else src / "i18n.tsx"
    zh = dictionary_keys(i18n.read_text(encoding="utf-8"))
    print(f"ZH dictionary: {len(zh)} entries")

    missing: dict[str, list[str]] = {}
    frozen: list[str] = []
    for f in sorted(list(src.rglob("*.tsx")) + list(src.rglob("*.ts"))):
        body = without_comments(f.read_text(encoding="utf-8"))
        rel = str(f.relative_to(src))
        for a, b in CALL.findall(body):
            key = unescape(a or b)
            if key and key not in zh:
                missing.setdefault(key, []).append(rel)
        for i, line in enumerate(body.splitlines(), 1):
            if "tr(" not in line or "=>" in line:
                continue
            if BINDING.match(line) or FIELD.match(line):
                frozen.append(f"{rel}:{i}: {line.strip()}")

    for key, where in sorted(missing.items()):
        print(f"[missing zh] {key[:120]!r}  <- {', '.join(sorted(set(where)))}")
    for line in frozen:
        print(f"[frozen tr]  {line}")
    print("missing %d · frozen %d" % (len(missing), len(frozen)))
    return 1 if (missing or frozen) else 0


if __name__ == "__main__":
    raise SystemExit(main())
