from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Any

from models import ActionRequest, ValidationResult


def build_transition_record(
    action_request: ActionRequest,
    validation: ValidationResult,
    *,
    pre_run_ledger: dict[str, Any],
    pre_task_ledger: dict[str, Any] | None = None,
    post_run_ledger: dict[str, Any] | None = None,
    post_task_ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    transition_id = f"tr_{uuid.uuid4().hex[:16]}"
    timestamp = action_request.timestamp or _now_iso()
    effective_task_id = _effective_task_id(action_request)
    return {
        "schema_version": "0.3.0",
        "transition_id": transition_id,
        "run_id": action_request.run_id,
        "task_id": effective_task_id,
        "timestamp": timestamp,
        "entity_type": "task" if _is_task_action(action_request.action) else "run",
        "action": action_request.action,
        "decision": validation.decision,
        "from_state": _state_snapshot(pre_run_ledger, pre_task_ledger),
        "to_state": _state_snapshot(post_run_ledger or pre_run_ledger, post_task_ledger or pre_task_ledger),
        "reason_code": validation.reason_code,
        "reason": validation.reason,
        "approval_category": action_request.payload.get("approval_category"),
        "trigger": {
            "source": action_request.trigger_source,
            "hook_name": action_request.hook_name,
            "event_name": action_request.event_name,
            "request_id": action_request.request_id,
        },
        "guard_results": [guard.as_dict() for guard in validation.guard_results],
        "effects": {
            "run_ledger_updated": post_run_ledger is not None,
            "task_ledger_updated": post_task_ledger is not None,
            "task_created": action_request.action == "create_task" and validation.decision in {"allowed", "noop"},
            "approval_created": action_request.action == "request_approval" and validation.decision in {"allowed", "noop"},
            "hard_stop_changed": action_request.action in {"mark_hard_stop", "clear_hard_stop"} and validation.decision in {"allowed", "noop"},
            "checkpoint_written": False,
            "artifact_refs_added": len(action_request.payload.get("artifact_refs", [])),
        },
        "artifact_refs": list(action_request.payload.get("artifact_refs", [])),
        "notes": action_request.reason,
    }


def _state_snapshot(run_ledger: dict[str, Any], task_ledger: dict[str, Any] | None) -> dict[str, Any]:
    snapshot = {
        "run_status": run_ledger.get("run_status"),
        "phase": run_ledger.get("current_phase"),
        "task_status": None,
        "task_group": None,
        "task_kind": None,
    }
    if task_ledger is not None:
        snapshot["task_status"] = task_ledger.get("status")
        snapshot["task_group"] = task_ledger.get("task_group")
        snapshot["task_kind"] = task_ledger.get("task_kind")
    return snapshot


def _is_task_action(action: str) -> bool:
    return action in {
        "create_task",
        "start_task",
        "move_task_to_waiting",
        "block_task",
        "retry_task",
        "complete_task",
        "fail_task",
        "abort_task",
        "noop_task",
    }


def _effective_task_id(action_request: ActionRequest) -> str | None:
    if action_request.action == "create_task":
        task_id = action_request.payload.get("task_id")
        return str(task_id) if task_id is not None else None
    return action_request.task_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
