#!/bin/bash
# Read-only Claude Code review driver.
#
# The tool set is the enforcement, not the prompt: only Read/Grep/Glob are
# exposed, so Edit/Write/Bash/WebFetch are not merely discouraged, they do not
# exist in the session. MCP servers are skipped so no third-party tool can
# slip in. Verified by probe: with Edit allowed the model does write files.
#
# Usage: claude_review.sh <out.json> <prompt-file> [repo-dir]
set -euo pipefail

OUT="${1:?usage: claude_review.sh <out.json> <prompt-file> [repo-dir]}"
PROMPT_FILE="${2:?usage: claude_review.sh <out.json> <prompt-file> [repo-dir]}"
REPO="${3:-/Users/neuroextra/team-agent}"

[ -f "$PROMPT_FILE" ] || { echo "no such prompt file: $PROMPT_FILE" >&2; exit 2; }
[ -d "$REPO" ] || { echo "no such repo: $REPO" >&2; exit 2; }

cd "$REPO"
start=$(date +%s)
claude -p "$(cat "$PROMPT_FILE")" \
  --tools "Read,Grep,Glob" \
  --strict-mcp-config \
  --output-format json \
  > "$OUT" 2> "${OUT%.json}.err"
rc=$?
end=$(date +%s)

echo "exit=$rc elapsed=$((end - start))s out=$OUT"
if [ -s "$OUT" ]; then
  /Users/neuroextra/.workbuddy/binaries/python/versions/3.13.12/bin/python3 - "$OUT" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as exc:
    print(f"[warn] unparsable json: {exc}")
    raise SystemExit(0)
print("denials:", d.get("permission_denials"))
print("cost_usd: %.4f turns: %s dur_ms: %s" % (
    d.get("total_cost_usd") or 0, d.get("num_turns"), d.get("duration_ms")))
PY
fi
