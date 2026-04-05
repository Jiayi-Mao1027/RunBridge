#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${1:-$(pwd)}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
USER_ROOT="$(cd -- "$SKILL_DIR/../../.." && pwd)"

ENSURE_HELPER="$USER_ROOT/.codex/protocol/bin/ensure_project_state.sh"
OWNED_HELPER="$USER_ROOT/.codex/protocol/bin/owned_processes.py"

if [[ ! -x "$ENSURE_HELPER" ]]; then
  echo "missing or non-executable ensure helper: $ENSURE_HELPER" >&2
  exit 2
fi

if [[ ! -f "$OWNED_HELPER" ]]; then
  echo "missing owned_processes helper: $OWNED_HELPER" >&2
  exit 2
fi

"$ENSURE_HELPER" "$REPO_ROOT"
python3 "$OWNED_HELPER" snapshot