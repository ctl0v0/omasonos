#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ID="io.github.ctl0v0.omasonos"
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}"
TARGET="${CONFIG_HOME}/omarchy/plugins/${PLUGIN_ID}"

for cmd in omarchy omarchy-shell; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "Missing required command: $cmd" >&2
    exit 1
  }
done

echo "Validating OmaSonos…"
omarchy plugin validate "$ROOT"

mkdir -p "$(dirname "$TARGET")"
tmp_target="${TARGET}.new.$$"
rm -rf "$tmp_target"
trap 'rm -rf "$tmp_target"' EXIT
mkdir -p "$tmp_target"
cp -a "$ROOT/." "$tmp_target/"
rm -rf "$tmp_target/.git" "$tmp_target/.pytest_cache"
find "$tmp_target" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$tmp_target" -type f -name '*.pyc' -delete
rm -rf "$TARGET"
mv "$tmp_target" "$TARGET"
trap - EXIT

echo "Installed local copy at $TARGET"
omarchy-shell shell rescanPlugins >/dev/null
omarchy plugin enable "$PLUGIN_ID"

echo
echo "OmaSonos is enabled. The first service start will create its isolated Python venv."
echo "If the bar does not refresh immediately, run: omarchy-shell shell rescanPlugins"
