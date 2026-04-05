#!/usr/bin/env bash
set -euo pipefail
python3 ~/.codex/protocol/bin/gpu_probe.py --requested "${1:-2}" --max-gpus 3 --json
