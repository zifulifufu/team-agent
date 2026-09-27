#!/usr/bin/env python3
"""Zero-reference candidates in a Python app: module-level names nobody mentions anywhere else.

This is the **mechanical half** of a dead-code sweep, and only that half. It greps, it does not
understand: an entry here means "the name appears exactly once in the whole corpus — at its own
definition", never "this is dead". A large share of the output is alive, and the skill's own notes
say why:

  * a parser's `handle_starttag` / `handle_data` are called **by the base class**, by name;
  * pydantic model classes are "used" by being the annotation on a route parameter;
  * a `# noqa: F401` import is deliberate (warming a stack, re-exporting for tests);
  * a flag set in the backend, handed over in a response and read by a front-end branch looks unused
    from Python alone.

So the output is a reading list, not a delete list. `--extra` is what keeps it honest for that last
point: point it at the front-end and the scripts too, or every backend value the UI reads by name
comes back as "unused".

Usage:
    python3 scripts/dead-code-sweep.py . --app backend/app --extra "desktop/src desktop/electron"
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

SKIP_DIRS = {"__pycache__", ".venv", "node_modules", ".git", "dist", "build", ".workbuddy"}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo", nargs="?", default=".", help="repository root")
    ap.add_argument("--app", default="backend/app", help="the package to scan for definitions")
    ap.add_argument("--extra", default="", help="more roots to search for *uses* (space separated)")
    ap.add_argument("--min-name", type=int, default=3, help="ignore names shorter than this")
    return ap.parse_args()


def walk(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix not in (".py", ".ts", ".tsx", ".js", ".cjs", ".mjs", ".json", ".md"):
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        yield p


def definitions(path: Path) -> list[tuple[str, str, int]]:
    """(kind, name, line) for every module-level name this file introduces.

    Methods are collected too — a class method nobody calls is one of the commonest kinds of dead
    code in a module of helpers, and it is invisible to a top-level-only scan.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []
    out: list[tuple[str, str, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(("def", node.name, node.lineno))
        elif isinstance(node, ast.ClassDef):
            out.append(("class", node.name, node.lineno))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(("method", sub.name, sub.lineno))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.append(("const", t.id, node.lineno))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.append(("const", node.target.id, node.lineno))
    return out


def main() -> int:
    args = parse_args()
    repo = Path(args.repo).resolve()
    app = repo / args.app
    if not app.is_dir():
        print(f"no such package: {app}", file=sys.stderr)
        return 2

    # One corpus string per name search, built once: the scan is O(names x corpus) with a regex,
    # which at this size is seconds — a per-name file walk would be minutes.
    roots = [app] + [repo / e for e in args.extra.split() if e]
    corpus: list[str] = []
    for root in roots:
        if not root.exists():
            print(f"warning: --extra root does not exist: {root}", file=sys.stderr)
            continue
        for p in walk(root):
            try:
                corpus.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    text = "\n".join(corpus)

    rows: list[tuple[str, str, str, int]] = []
    for path in sorted(app.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        for kind, name, line in definitions(path):
            if len(name) < args.min_name or name.startswith("_"):
                continue
            hits = len(re.findall(rf"\b{re.escape(name)}\b", text))
            if hits <= 1:                      # only the definition itself
                rows.append((kind, name, f"{path.relative_to(repo)}:{line}", hits))

    if not rows:
        print("no zero-reference candidates")
        return 0
    width = max(len(r[2]) for r in rows)
    for kind, name, where, hits in rows:
        print(f"{kind:<7} {where:<{width}}  {name}")
    print(f"\n{len(rows)} candidate(s) — read each one before deleting anything; "
          f"see the dead-code-sweep skill's 'must not delete' list.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
