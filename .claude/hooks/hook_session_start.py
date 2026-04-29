from __future__ import annotations

import re
import uuid

from common import invoke_runtime_event, now_iso, read_hook_input, runtime_runs_root, write_active_run


def main() -> int:
    payload = read_hook_input()
    timestamp = now_iso()
    session_id = _session_id(payload)
    run_id = _new_run_id(timestamp, session_id)
    main_session_id = session_id or run_id

    active = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "main_session_id": main_session_id,
        "session_id": session_id,
        "created_at": timestamp,
        "cwd": payload.get("cwd"),
        "hook_event_name": payload.get("hook_event_name"),
        "runtime_runs_root": str(runtime_runs_root()),
    }
    event = {
        "run_id": run_id,
        "main_session_id": main_session_id,
        "agent_id": "hook.session_start",
        "agent_type": "hook",
        "event_kind": "session_started",
        "timestamp": timestamp,
        "payload": active,
    }
    code, result, stderr = invoke_runtime_event(event, persist=True)
    workflow = result.get("workflow_result", {}) if isinstance(result, dict) else {}
    if code != 0 or not workflow.get("ok", False):
        print(stderr or result)
        return 2
    write_active_run(active)
    return 0


def _new_run_id(timestamp: str, session_id: str) -> str:
    stamp = re.sub(r"[^0-9A-Za-z]", "", timestamp)[:15]
    session_part = re.sub(r"[^0-9A-Za-z_-]", "", session_id)[:12]
    suffix = uuid.uuid4().hex[:8]
    if session_part:
        return f"run_{stamp}_{session_part}_{suffix}"
    return f"run_{stamp}_{suffix}"


def _session_id(payload: dict) -> str:
    for key in ("session_id", "sessionId", "main_session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("session", "context", "transcript"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = _session_id(nested)
            if found:
                return found
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
