#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-$(pwd)}"
mkdir -p "$ROOT/specs"
mkdir -p "$ROOT/artifacts/runs"
mkdir -p "$ROOT/artifacts/tmp"
mkdir -p "$ROOT/ops/contracts"
mkdir -p "$ROOT/ops/templates"

for f in mission.md current_run.md learned_constraints.md; do
  if [[ ! -f "$ROOT/specs/$f" ]]; then
    cat > "$ROOT/specs/$f" <<EOF
# ${f%.md}

Bootstrap placeholder created by ensure_project_state.sh.
Replace with the real project content.
EOF
  fi
done

echo "Initialized bootstrap state under: $ROOT"
