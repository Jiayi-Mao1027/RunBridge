#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${1:-$(pwd)}"
PACKET_PATH="${2:-}"
PHASE="${3:-}"
OUTPUT_DIR="${4:-}"
RUN_ID="${5:-}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
USER_ROOT="$(cd -- "$SKILL_DIR/../../.." && pwd)"
RUNNER="$USER_ROOT/.codex/protocol/bin/claude_skill_runner.py"

if [[ -z "$PACKET_PATH" ]]; then
  echo "missing packet path" >&2
  exit 2
fi

if [[ ! -f "$RUNNER" ]]; then
  echo "missing runner: $RUNNER" >&2
  exit 2
fi

CLAUDE_MAX_THINKING_TOKENS="${CLAUDE_MAX_THINKING_TOKENS:-10000}"
CLAUDE_MAX_TURNS="${CLAUDE_MAX_TURNS:-8}"
CLAUDE_PERMISSION_MODE="${CLAUDE_PERMISSION_MODE:-acceptEdits}"
CLAUDE_EXTRA_ARGS="${CLAUDE_EXTRA_ARGS:-}"
CLAUDE_ADD_DIRS="${CLAUDE_ADD_DIRS:-}"

ARGS=(
  --role "refresher"
  --skill-dir "$SKILL_DIR"
  --repo-root "$REPO_ROOT"
  --packet "$PACKET_PATH"
  --max-thinking-tokens "$CLAUDE_MAX_THINKING_TOKENS"
  --max-turns "$CLAUDE_MAX_TURNS"
  --permission-mode "$CLAUDE_PERMISSION_MODE"

  --allowed-tool "Read"
  --allowed-tool "Grep"
  --allowed-tool "Glob"
  --allowed-tool "LS"
  --allowed-tool "NotebookRead"
  --allowed-tool "Edit"
  --allowed-tool "MultiEdit"
  --allowed-tool "Write"
  --allowed-tool "NotebookEdit"
  --allowed-tool "TodoWrite"

  --disallowed-tool "Bash"
  --disallowed-tool "Task"
  --disallowed-tool "WebFetch"
  --disallowed-tool "WebSearch"
)

if [[ -n "$PHASE" ]]; then
  ARGS+=(--phase "$PHASE")
fi
if [[ -n "$OUTPUT_DIR" ]]; then
  ARGS+=(--output-dir "$OUTPUT_DIR")
fi
if [[ -n "$RUN_ID" ]]; then
  ARGS+=(--run-id "$RUN_ID")
fi

if [[ -d "$REPO_ROOT/specs" ]]; then
  ARGS+=(--add-dir "$REPO_ROOT/specs")
fi
if [[ -d "$REPO_ROOT/artifacts" ]]; then
  ARGS+=(--add-dir "$REPO_ROOT/artifacts")
fi
if [[ -f "$REPO_ROOT/AGENTS.md" ]]; then
  ARGS+=(--add-dir "$REPO_ROOT")
fi

if [[ -n "$CLAUDE_ADD_DIRS" ]]; then
  IFS=':' read -r -a EXTRA_DIR_ARRAY <<< "$CLAUDE_ADD_DIRS"
  for d in "${EXTRA_DIR_ARRAY[@]}"; do
    if [[ -n "$d" && -e "$d" ]]; then
      ARGS+=(--add-dir "$d")
    fi
  done
fi

if [[ -n "$CLAUDE_EXTRA_ARGS" ]]; then
  # shellcheck disable=SC2206
  EXTRA_ARGS_ARRAY=($CLAUDE_EXTRA_ARGS)
  for x in "${EXTRA_ARGS_ARRAY[@]}"; do
    ARGS+=(--extra-arg "$x")
  done
fi

python3 "$RUNNER" "${ARGS[@]}"
