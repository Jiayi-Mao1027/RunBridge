from __future__ import annotations

from pathlib import Path

from common import detect_run_id, now_iso, read_hook_input, runtime_runs_root, write_json


def main() -> int:
    payload = read_hook_input()
    run_id = detect_run_id(payload)
    if not run_id:
        return 0

    snapshot = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "ended_at": now_iso(),
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
