#!/bin/bash
# 在真机上完整跑一遍后端测试,结果写到 ~/team-agent/_sync/pytest_real.txt(不改任何源码)
OUT="$HOME/team-agent/_sync/pytest_real.txt"
cd "$HOME/team-agent/backend" || exit 1
rm -rf /tmp/ta-pyt-real && mkdir -p /tmp/ta-pyt-real
{
  echo "date: $(date)"; echo "python: $(../.venv/bin/python -V 2>&1) arch=$(../.venv/bin/python -c 'import platform;print(platform.machine())')"
  echo "sqlite: $(../.venv/bin/python -c 'import sqlite3;print(sqlite3.sqlite_version)')"
  ../.venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/tmp/ta-pyt-real 2>&1 | tail -80
  echo "EXIT=${PIPESTATUS[0]}"
} > "$OUT" 2>&1
