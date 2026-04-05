#!/usr/bin/env bash
set -euo pipefail
python3 ~/.codex/protocol/bin/owned_processes.py check "$1"
