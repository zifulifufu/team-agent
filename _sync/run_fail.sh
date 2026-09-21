#!/bin/bash
OUT="$HOME/team-agent/_sync/pytest_fail.txt"
cd "$HOME/team-agent/backend" || exit 1
rm -rf /tmp/ta-pyt-real2 && mkdir -p /tmp/ta-pyt-real2
{
  ../.venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/tmp/ta-pyt-real2 --tb=short \
    "tests/test_api.py::test_plugin_registration" "tests/test_data_flows.py::test_restore_twice_in_a_row_keeps_both_safety_copies" \
    "tests/test_permissions.py::test_mcp_json_import_parse_and_add" 2>&1 | tail -120
  echo "EXIT=${PIPESTATUS[0]}"
} > "$OUT" 2>&1
