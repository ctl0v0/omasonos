#!/bin/bash
set -euo pipefail

PLUGIN_ID="io.github.ctl0v0.omasonos"
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

for cmd in omarchy omarchy-shell python3 jq; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "Missing required command: $cmd" >&2
    exit 1
  }
done

echo "== Manifest =="
"$ROOT/scripts/validate-plugin.sh"
echo "ok"

echo
echo "== QML =="
if [[ -n ${OMARCHY_PATH:-} ]] && { command -v qmllint >/dev/null 2>&1 || [[ -x /usr/lib/qt6/bin/qmllint ]]; }; then
  "$ROOT/scripts/lint-qml.sh"
else
  echo "qmllint or OMARCHY_PATH unavailable; skipping QML lint"
fi

echo
echo "== Python tests =="
if python3 -c 'import pytest' >/dev/null 2>&1; then
  (cd "$ROOT" && python3 -m pytest -q)
else
  echo "pytest not installed; skipping unit tests"
fi

echo
echo "== Omarchy registration =="
plugins_json="$(omarchy-shell shell listPlugins)"
if printf '%s' "$plugins_json" | grep -Fq "$PLUGIN_ID"; then
  echo "$PLUGIN_ID is discovered by omarchy-shell"
else
  echo "$PLUGIN_ID is not present in omarchy-shell listPlugins" >&2
  echo "Run ./scripts/install-local.sh first." >&2
  exit 1
fi

echo
echo "== Sonos backend =="
python3 "$ROOT/scripts/smoke-backend.py"
