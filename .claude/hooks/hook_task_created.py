from __future__ import annotations

from common import (
    detect_run_id,
    invoke_runtime,
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

    task_group = str(embedded.get("task_group") or "").strip()
    task_kind = str(embedded.get("task_kind") or "").strip()
    objective = str(embedded.get("objective") or subject or description).strip()

    if not task_group or not task_kind or not objective:
        return simple_block(
            "TaskCreated blocked: task_description must include JSON with task_group, task_kind, and objective."
        )

    request = {
        "run_id": run_id,
        "action": "create_task",
        "task_id": None,
        "payload": {
            "task_id": task_id,
            "task_group": task_group,
            "task_kind": task_kind,
            "objective": objective,
            "phase_gate": bool(embedded.get("phase_gate", False)),
            "handoff_to_group": embedded.get("handoff_to_group"),
            "depends_on": embedded.get("depends_on", []),
            "dependents": embedded.get("dependents", []),
            "blocked_by_task_ids": embedded.get("blocked_by_task_ids", []),
            "approval_ref": embedded.get("approval_ref"),
            "scope": embedded.get("scope", {}),
            "inputs": embedded.get("inputs", []),
            "acceptance_contract": embedded.get("acceptance_contract", {
                "required_outputs": [],
                "validation_requirements": [],
                "phase_exit_relevant": bool(embedded.get("phase_gate", False)),
            }),
            "required_artifacts": embedded.get("required_artifacts", []),
            "produced_artifacts": embedded.get("produced_artifacts", []),
            "blocking_reason": embedded.get("blocking_reason"),
            "completion_checks": embedded.get("completion_checks", {
                "required_outputs_present": False,
                "required_artifacts_present": False,
                "validation_passed": False,
                "missing_outputs": [],
                "missing_artifacts": [],
                "failed_validations": [],
                "notes": [],
            }),
            "completion_effect": embedded.get("completion_effect", {
                "may_advance_phase": bool(embedded.get("phase_gate", False)),
                "may_spawn_next_tasks": True,
                "next_default_group": embedded.get("handoff_to_group"),
                "next_task_candidates": [],
            }),
            "approval_category": embedded.get("approval_category"),
            "retry_count": int(embedded.get("retry_count", 0)),
            "attempts": embedded.get("attempts", []),
            "owner": embedded.get("owner"),
        },
        "reason": f"hook TaskCreated: {subject}",
        "requester": "hook.task_created",
        "timestamp": now_iso(),
        "trigger_source": "hook",
        "hook_name": "TaskCreated",
        "event_name": payload.get("hook_event_name"),
        "request_id": task_id,
    }

    code, result, stderr = invoke_runtime(request, persist=True)
    if code != 0:
        return simple_block(f"TaskCreated blocked: runtime invocation failed. {stderr or result!r}")

    dispatch = result.get("dispatch_result", {})
    if not dispatch.get("ok", False):
        return simple_block(f"TaskCreated blocked: {dispatch.get('decision')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
