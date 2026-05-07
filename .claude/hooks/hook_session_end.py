from __future__ import annotations

from pathlib import Path

from common import detect_run_id, emit_observer_record, now_iso, observer_binding, read_hook_input, runtime_runs_root, write_json


def main() -> int:
    payload = read_hook_input()
    timestamp = now_iso()
    binding = observer_binding(payload)
    emit_observer_record(
        "session_events",
        {
            "timestamp": timestamp,
            **binding,
            "event_type": "session_ended",
            "message_preview": str(payload.get("reason") or "session ended")[:500],
            "cwd": payload.get("cwd"),
            "project_root": payload.get("project_root"),
        },
    )
    run_id = detect_run_id(payload)
    if not run_id:
        return 0

    snapshot = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "ended_at": timestamp,
        "reason": payload.get("reason"),
        "session_id": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        "hook_event_name": payload.get("hook_event_name"),
    }

    out = runtime_runs_root() / run_id / "session_end_snapshot.json"
    write_json(out, snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
