#!/bin/bash
set -euo pipefail

PLUGIN_ID="io.github.ctl0v0.omasonos"
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${HOME}/.config/omarchy/plugins/${PLUGIN_ID}"

for cmd in jq omarchy omarchy-shell; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "Missing required command: $cmd" >&2
    exit 1
  }
done

mkdir -p "$(dirname "$TARGET")"
tmp_target="$(mktemp -d "$HOME/.config/omarchy/.omasonos-install.XXXXXX")"
trap 'rm -rf "$tmp_target"' EXIT
"$ROOT/scripts/stage-plugin.sh" "$tmp_target"

echo "Validating OmaSonos…"
omarchy plugin validate "$tmp_target"

old_target="${tmp_target}.old"
if [[ -e $TARGET ]]; then
  mv "$TARGET" "$old_target"
fi
if ! mv "$tmp_target" "$TARGET"; then
  [[ ! -e $old_target ]] || mv "$old_target" "$TARGET"
  echo "Could not replace the installed OmaSonos plugin" >&2
  exit 1
fi
rm -rf "$old_target"
trap - EXIT

echo "Installed local copy at $TARGET"
omarchy-shell shell rescanPlugins >/dev/null

discovered=false
for ((attempt = 0; attempt < 50; attempt++)); do
  if omarchy-shell shell listPlugins | jq -e --arg id "$PLUGIN_ID" \
    'any(.[]; .id == $id)' >/dev/null; then
    discovered=true
    break
  fi
  sleep 0.1
done
[[ $discovered == true ]] || {
  echo "OmaSonos was installed but not discovered by omarchy-shell" >&2
  exit 1
}

omarchy plugin enable "$PLUGIN_ID"

echo
echo "OmaSonos is enabled. The first service start will create its isolated Python venv."
echo "If the bar does not refresh immediately, run: omarchy-shell shell rescanPlugins"
