from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from models import ActionRequest, LoadedState


def apply_task_action(action_request: ActionRequest, state: LoadedState) -> None:
    now = action_request.timestamp or _now_iso()
    action = action_request.action

    if action == "create_task":
        _create_task(action_request, state, now)
        return

    task_id = action_request.task_id
    if task_id is None:
        raise ValueError(f"{action} requires task_id")

    task = state.task_ledgers[task_id]
    task["updated_at"] = now

    if action == "start_task":
        task["status"] = "in_progress"
        return

    if action == "move_task_to_waiting":
        reason_code = action_request.payload.get("reason_code", "dependency_unfinished")
        task["status"] = "waiting_on_approval" if reason_code == "approval_required" else "waiting_on_dependency"
        task["blocked_by_task_ids"] = list(action_request.payload.get("blocked_by_task_ids", []))
        task["blocking_reason"] = {
            "code": reason_code,
            "details": action_request.payload.get("details", ""),
        }
        return

    if action == "block_task":
        task["status"] = "blocked"
        task["blocking_reason"] = {
            "code": action_request.payload.get("code", "other"),
            "details": action_request.payload.get("details", ""),
        }
        return

    if action == "retry_task":
        task["status"] = "ready"
        task["retry_count"] = int(task.get("retry_count", 0)) + 1
        task["blocking_reason"] = None
        task["blocked_by_task_ids"] = []
        return

    if action == "complete_task":
        task["status"] = "completed"
        task["produced_artifacts"] = deepcopy(
            action_request.payload.get("produced_artifacts", task.get("produced_artifacts", []))
        )
        if "completion_checks" in action_request.payload:
            task["completion_checks"] = deepcopy(action_request.payload["completion_checks"])
        task["closed_at"] = now
        return

    if action == "fail_task":
        retryable = bool(action_request.payload.get("retryable", False))
        task["status"] = "retryable_failure" if retryable else "failed"
        task["blocking_reason"] = {
            "code": action_request.payload.get("code", "runtime_error"),
            "details": action_request.payload.get("details", ""),
        }
        if not retryable:
            task["closed_at"] = now
        return

    if action == "abort_task":
        task["status"] = "aborted"
        task["closed_at"] = now
        return

    if action == "noop_task":
        task["status"] = "noop"
        task["closed_at"] = now
        return

    raise ValueError(f"Unsupported task action: {action}")


def _create_task(action_request: ActionRequest, state: LoadedState, now: str) -> None:
    payload = action_request.payload
    task_id = str(payload["task_id"])
    state.task_ledgers[task_id] = {
        "schema_version": "0.3.0",
        "task_id": task_id,
        "run_id": action_request.run_id,
        "task_group": payload["task_group"],
        "task_kind": payload["task_kind"],
        "objective": payload["objective"],
        "status": payload.get("status", "created"),
        "phase_gate": bool(payload.get("phase_gate", False)),
        "spawned_by_task_id": action_request.task_id,
        "handoff_to_group": payload.get("handoff_to_group"),
        "depends_on": list(payload.get("depends_on", [])),
        "dependents": list(payload.get("dependents", [])),
        "blocked_by_task_ids": list(payload.get("blocked_by_task_ids", [])),
        "approval_ref": payload.get("approval_ref"),
        "scope": deepcopy(payload.get("scope", {})),
        "inputs": deepcopy(payload.get("inputs", [])),
        "acceptance_contract": deepcopy(
            payload.get(
                "acceptance_contract",
                {
                    "required_outputs": [],
                    "validation_requirements": [],
                    "phase_exit_relevant": bool(payload.get("phase_gate", False)),
                },
            )
        ),
        "required_artifacts": deepcopy(payload.get("required_artifacts", [])),
        "produced_artifacts": deepcopy(payload.get("produced_artifacts", [])),
        "blocking_reason": deepcopy(payload.get("blocking_reason")),
        "completion_checks": deepcopy(
            payload.get(
                "completion_checks",
                {
                    "required_outputs_present": False,
                    "required_artifacts_present": False,
                    "validation_passed": False,
                    "missing_outputs": [],
                    "missing_artifacts": [],
                    "failed_validations": [],
                    "notes": [],
                },
            )
        ),
        "completion_effect": deepcopy(
            payload.get(
                "completion_effect",
                {
                    "may_advance_phase": bool(payload.get("phase_gate", False)),
                    "may_spawn_next_tasks": True,
                    "next_default_group": payload.get("handoff_to_group"),
                    "next_task_candidates": [],
                },
            )
        ),
        "approval_category": payload.get("approval_category"),
        "retry_count": int(payload.get("retry_count", 0)),
        "attempts": deepcopy(payload.get("attempts", [])),
        "owner": payload.get("owner"),
        "created_at": now,
        "updated_at": now,
        "closed_at": None,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
