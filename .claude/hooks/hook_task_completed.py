from __future__ import annotations

from common import (
    control_binding_value,
    control_main_session_id,
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
        return simple_block("TaskCompleted blocked: missing run_id / CLAUDE_CONTROL_RUN_ID.")

    description = str(payload.get("task_description") or payload.get("description") or "").strip()
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        return simple_block("TaskCompleted blocked: missing task_id from hook payload.")

    embedded = parse_embedded_json(description)
    event = {
        "run_id": run_id,
        "main_session_id": control_main_session_id(payload),
        "sub_session_id": control_binding_value("sub_session_id", payload, embedded=embedded),
        "bridge_window_id": control_binding_value("bridge_window_id", payload, embedded=embedded),
        "team_id": control_binding_value("team_id", payload, embedded=embedded),
        "task_id": task_id,
        "agent_id": payload.get("agent_id") or "hook.task_completed",
        "agent_type": payload.get("agent_type") or "hook",
        "event_kind": "completion_contract_satisfied",
        "timestamp": now_iso(),
        "payload": {
            "completion_contract": embedded.get("completion_contract"),
            "completion_evidence": embedded.get("completion_evidence"),
            "reports": embedded.get("reports", []),
            "artifact_refs": embedded.get("artifact_refs", embedded.get("produced_artifacts", [])),
            "completion_checks": embedded.get("completion_checks", {}),
        },
    }

    code, result, stderr = invoke_runtime_event(event, persist=True)
    if code != 0:
        return simple_block(f"TaskCompleted blocked: runtime invocation failed. {stderr or result!r}")

    workflow = result.get("workflow_result", {})
    if not workflow.get("ok", False):
        check = workflow.get("check_result", {})
        rejected_event = dict(event)
        rejected_event["event_kind"] = "completion_contract_rejected"
        rejected_event.pop("event_id", None)
        rejected_event["timestamp"] = now_iso()
        rejected_event["payload"] = {
            **event["payload"],
            "missing_contract_items": check.get("reasons", []),
        }
        invoke_runtime_event(rejected_event, persist=True)
        return simple_block(f"TaskCompleted blocked: {check.get('decision')} {check.get('reasons')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
