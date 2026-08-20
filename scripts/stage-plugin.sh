#!/bin/bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-}"

if [[ -z $TARGET ]]; then
  printf 'Usage: %s <empty-target-directory>\n' "$0" >&2
  exit 1
fi

mkdir -p "$TARGET"
while IFS= read -r -d '' path; do
  [[ -e "$ROOT/$path" || -L "$ROOT/$path" ]] || continue
  mkdir -p "$TARGET/$(dirname -- "$path")"
  cp -a -- "$ROOT/$path" "$TARGET/$path"
done < <(git -C "$ROOT" ls-files --cached --others --exclude-standard -z)
