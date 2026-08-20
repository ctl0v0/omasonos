#!/bin/bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
QMLLINT_BIN="${QMLLINT:-}"

if [[ -z $QMLLINT_BIN ]]; then
  QMLLINT_BIN="$(command -v qmllint || true)"
fi
if [[ -z $QMLLINT_BIN && -x /usr/lib/qt6/bin/qmllint ]]; then
  QMLLINT_BIN=/usr/lib/qt6/bin/qmllint
fi
if [[ -z $QMLLINT_BIN || ! -x $QMLLINT_BIN ]]; then
  echo "qmllint not found" >&2
  exit 1
fi
if [[ -z ${OMARCHY_PATH:-} || ! -d $OMARCHY_PATH/shell ]]; then
  echo "OMARCHY_PATH does not point to an Omarchy checkout" >&2
  exit 1
fi

IMPORT_ROOT="$(mktemp -d)"
trap 'rm -rf "$IMPORT_ROOT"' EXIT
ln -s "$OMARCHY_PATH/shell" "$IMPORT_ROOT/qs"

"$QMLLINT_BIN" \
  --missing-property disable \
  --signal-handler-parameters disable \
  --unqualified disable \
  --unused-imports disable \
  -I "$IMPORT_ROOT" \
  "$ROOT/Service.qml" "$ROOT/Widget.qml"
