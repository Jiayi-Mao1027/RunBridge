#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
USER_ROOT="$(cd -- "$SKILL_DIR/../../.." && pwd)"
python3 "$USER_ROOT/.codex/protocol/bin/owned_processes.py" check "$1"
