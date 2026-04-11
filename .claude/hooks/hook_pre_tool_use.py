from __future__ import annotations

import json
from pathlib import Path

from common import control_root, detect_run_id, pretool_deny, read_hook_input, runtime_runs_root


DANGEROUS_TOOL_NAMES = {"Write", "Edit", "MultiEdit", "Bash"}


def load_run_ledger(run_id: str) -> dict:
    path = runtime_runs_root() / run_id / "run_ledger.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    payload = read_hook_input()
    run_id = detect_run_id(payload)
    if not run_id:
        return 0

    run = load_run_ledger(run_id)
    if not run:
        return 0

    tool_name = str(payload.get("tool_name") or "").strip()
    hard_stop = run.get("hard_stop", {})
    if hard_stop.get("active") and tool_name in DANGEROUS_TOOL_NAMES:
        return pretool_deny(f"hard_stop.active=true blocks tool {tool_name}")

    run_status = str(run.get("run_status") or "")
    if run_status in {"blocked", "completed", "aborted", "failed"} and tool_name in DANGEROUS_TOOL_NAMES:
        return pretool_deny(f"run_status={run_status} blocks tool {tool_name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
