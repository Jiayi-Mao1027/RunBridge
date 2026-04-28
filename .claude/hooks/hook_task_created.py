from __future__ import annotations

from common import (
    detect_run_id,
    invoke_runtime_event,
    now_iso,
    parse_embedded_json,
    read_hook_input,
    simple_block,
)


def main() -> int:
    payload = read_hook_input()
    run_id = detect_run_id(payload)
    if not run_id:
        return simple_block("TaskCreated blocked: missing run_id / CLAUDE_CONTROL_RUN_ID.")

    subject = str(payload.get("task_subject") or payload.get("subject") or "").strip()
    description = str(payload.get("task_description") or payload.get("description") or "").strip()
    task_id = str(payload.get("task_id") or "").strip()
    embedded = parse_embedded_json(description)

    if not task_id:
        return simple_block("TaskCreated blocked: missing task_id from hook payload.")

    event = {
        "run_id": run_id,
        "main_session_id": payload.get("main_session_id") or payload.get("session_id"),
        "sub_session_id": payload.get("sub_session_id") or embedded.get("sub_session_id"),
        "bridge_window_id": payload.get("bridge_window_id") or embedded.get("bridge_window_id"),
        "team_id": payload.get("team_id") or embedded.get("team_id"),
        "task_id": task_id,
        "agent_id": payload.get("agent_id") or "hook.task_created",
        "agent_type": payload.get("agent_type") or "hook",
        "event_kind": "taskcreated_hook_accepted",
        "timestamp": now_iso(),
        "payload": {
            "task_id": task_id,
            "task_subject": subject,
            "task_description": description,
            "task_spec": embedded.get("task_spec", {}),
            "team_spec": embedded.get("team_spec", {}),
            "task_team_mapping": embedded.get("task_team_mapping", {}),
            "teammate_ids": embedded.get("teammate_ids", []),
        },
    }

    code, result, stderr = invoke_runtime_event(event, persist=True)
    if code != 0:
        return simple_block(f"TaskCreated blocked: runtime invocation failed. {stderr or result!r}")

    workflow = result.get("workflow_result", {})
    if not workflow.get("ok", False):
        check = workflow.get("check_result", {})
        denied_event = dict(event)
        denied_event["event_kind"] = "taskcreated_hook_denied"
        denied_event.pop("event_id", None)
        denied_event["timestamp"] = now_iso()
        denied_event["payload"] = {
            **event["payload"],
            "reasons": check.get("reasons", []),
        }
        invoke_runtime_event(denied_event, persist=True)
        return simple_block(f"TaskCreated blocked: {check.get('decision')} {check.get('reasons')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
