from __future__ import annotations

import json
from pathlib import Path

from common import detect_run_id, emit_observer_record, is_bridge_child_session, now_iso, observer_binding, read_hook_input, runtime_runs_root, stop_block


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
    binding = observer_binding(payload)
    emit_observer_record(
        "session_events",
        {
            "timestamp": now_iso(),
            **binding,
            "event_type": "stop",
            "message_preview": "stop requested",
            "cwd": payload.get("cwd"),
            "project_root": payload.get("project_root"),
        },
    )

    if is_bridge_child_session():
        return 0

    if payload.get("stop_hook_active") is True:
        return 0

    run_id = detect_run_id(payload)
    if not run_id:
        return 0

    run = load_run_ledger(run_id)
    if not run:
        return 0

    hard_stop = run.get("hard_stop", {})
    if hard_stop.get("active"):
        return stop_block("Cannot stop yet: hard_stop is active and requires follow-up.")

    approval_state = run.get("approval_state", {})
    if approval_state.get("pending"):
        return stop_block("Cannot stop yet: approval_state.pending=true and requires resolution.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
