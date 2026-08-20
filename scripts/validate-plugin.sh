#!/bin/bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

"$ROOT/scripts/stage-plugin.sh" "$STAGE"

if [[ -n ${OMARCHY_PLUGIN_VALIDATOR:-} ]]; then
  "$OMARCHY_PLUGIN_VALIDATOR" "$STAGE"
elif command -v omarchy >/dev/null 2>&1; then
  omarchy plugin validate "$STAGE"
else
  echo "Omarchy plugin validator not found" >&2
  exit 1
fi
