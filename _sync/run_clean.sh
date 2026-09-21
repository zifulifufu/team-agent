#!/bin/bash
# 用干净环境(不带 WorkBuddy 的 Python 沙箱垫片 sitecustomize)完整跑后端测试
OUT="$HOME/team-agent/_sync/pytest_clean.txt"
cd "$HOME/team-agent/backend" || exit 1
rm -rf /tmp/ta-pyt-clean && mkdir -p /tmp/ta-pyt-clean
PY=../.venv/bin/python
CLEAN=(env -i HOME="$HOME" USER="$USER" PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin" LANG=en_US.UTF-8 TMPDIR=/tmp/)
{
  echo "date: $(date)"
  "${CLEAN[@]}" $PY -I -c "import sys,platform,sqlite3;print('python',sys.version.split()[0],platform.machine(),'sqlite',sqlite3.sqlite_version);print('sitecustomize loaded:', 'sitecustomize' in sys.modules, getattr(sys.modules.get('sitecustomize'),'__file__',''))"
  "${CLEAN[@]}" $PY -I -m pytest -q -p no:cacheprovider --basetemp=/tmp/ta-pyt-clean 2>&1 | tail -60
  echo "EXIT=${PIPESTATUS[0]}"
} > "$OUT" 2>&1
