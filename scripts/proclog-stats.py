"""Where the process log stands, per group — is the ledger converging or only growing?

Run it against one or more `流程日志.md` / `process-log.md` files:

    .venv/bin/python scripts/proclog-stats.py ~/.team-agent/workspaces/*/流程日志.md

Why this exists: the ledger is the only place that records *which step of the flow* leaks, and the
question that decides whether it is worth reading is not "how many entries" but **"how many distinct
defects"**. Those two numbers being equal means every entry is a fresh instance, i.e. nothing is being
counted, and the "this keeps happening" signal the design is built around is not there.

⚠️ `unique keys / records` is the number to watch. A ratio near 1 means the key is keyed by the
*instance* (which file, which call) rather than by the *cause*, and `seen` can never rise above 1.
"""
from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

# Names that say the deliverable was itself a check: "the review report was never written" is a
# different — and much more fixable — defect than "the video was never rendered".
VERIFY_WORDS = ("复核", "验收", "核验", "诊断", "报告", "记录", "对账", "说明", "建议", "review", "verify")


def load(path: Path) -> list[dict]:
    txt = path.read_text(encoding="utf-8")
    out = []
    for block in re.split(r"^## ", txt, flags=re.M)[1:]:
        rec = {"_title": block.split("\n", 1)[0]}
        for m in re.finditer(r"^- (\w+): (.*)$", block, flags=re.M):
            rec[m.group(1)] = m.group(2).strip()
        out.append(rec)
    return out


def report(path: Path) -> None:
    recs = load(path)
    keys = [r.get("key", "") for r in recs]
    dup = 1 - len(set(keys)) / max(1, len(recs))
    print(f"===== {path.parent.name}  共 {len(recs)} 条")
    print("  status:", dict(collections.Counter(r.get("status", "?") for r in recs)))
    print(f"  唯一 key: {len(set(keys))} / {len(recs)}  → 实例化程度 {dup * 100:.1f}%")
    print("  seen 分布:", dict(sorted(collections.Counter(r.get("seen", "1") for r in recs).items())))
    named = [k.split(":", 1)[1] for k in keys if k.startswith("missing-file:")]
    if named:
        ext = collections.Counter(Path(n.split(":")[0]).suffix or "(无扩展名)" for n in named)
        checks = sum(1 for n in named if any(w in n for w in VERIFY_WORDS))
        print(f"  missing-file {len(named)} 条：按扩展名 {dict(ext)}")
        print(f"    其中名字像复核/验收/诊断/报告类：{checks} 条（{checks * 100 // max(1, len(named))}%）")
    print()


def main() -> int:
    args = [Path(a) for a in sys.argv[1:]]
    if not args:
        print(__doc__)
        return 2
    for p in args:
        if not p.is_file():
            print(f"（跳过，不是文件：{p}）")
            continue
        report(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
