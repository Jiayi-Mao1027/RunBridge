from __future__ import annotations

from common import detect_run_id, invoke_runtime_event, now_iso, read_hook_input, simple_block


def main() -> int:
    payload = read_hook_input()
    run_id = detect_run_id(payload)
    if not run_id:
        return simple_block("TeamIdle blocked: runtime run binding missing; SessionStart active-run is required.")

    timed_out = bool(payload.get("timed_out") or payload.get("timeout") or payload.get("owned_process_lost"))
    event = {
        "run_id": run_id,
        "main_session_id": payload.get("main_session_id") or payload.get("session_id"),
        "sub_session_id": payload.get("sub_session_id"),
        "bridge_window_id": payload.get("bridge_window_id"),
        "team_id": payload.get("team_id"),
        "task_id": payload.get("task_id"),
        "agent_id": payload.get("agent_id") or "hook.team_idle",
        "agent_type": payload.get("agent_type") or "hook",
        "event_kind": "wait_timeout_or_process_lost" if timed_out else "team_idle_waiting",
        "timestamp": now_iso(),
        "payload": {
            "wait_reason": payload.get("wait_reason", "team_idle"),
            "owned_process_refs": payload.get("owned_process_refs", []),
            "last_heartbeat_at": payload.get("last_heartbeat_at") or now_iso(),
            "timeout_policy": payload.get("timeout_policy", {}),
            "artifact_probe": payload.get("artifact_probe", {}),
            "partial_reports": payload.get("partial_reports", []),
            "partial_artifact_refs": payload.get("partial_artifact_refs", []),
            "evidence": payload.get("evidence"),
        },
    }

    code, result, stderr = invoke_runtime_event(event, persist=True)
    if code != 0:
        return simple_block(f"TeamIdle blocked: runtime invocation failed. {stderr or result!r}")
    workflow = result.get("workflow_result", {})
    if not workflow.get("ok", False):
        check = workflow.get("check_result", {})
        return simple_block(f"TeamIdle blocked: {check.get('decision')} {check.get('reasons')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
