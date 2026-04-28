from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from models import ActionRequest, LoadedState


def apply_run_action(action_request: ActionRequest, state: LoadedState) -> None:
    now = action_request.timestamp or _now_iso()
    run = state.run_ledger
    run["updated_at"] = now
    action = action_request.action

    if action == "advance_phase":
        target_phase = action_request.payload["target_phase"]
        _close_current_phase(run, now, exit_status="completed", exit_reason=action_request.reason)
        _enter_phase(run, target_phase, now, action_request.reason, action_request.task_id)
        return

    if action == "reroute_phase":
        target_phase = action_request.payload["target_phase"]
        _close_current_phase(run, now, exit_status="rerouted", exit_reason=action_request.reason)
        _enter_phase(run, target_phase, now, action_request.reason, action_request.task_id)
        return

    if action == "pause_run":
        run["run_status"] = "paused"
        return

    if action == "resume_run":
        run["run_status"] = "in_progress"
        return

    if action == "request_approval":
        approval_record = {
            "approval_id": action_request.payload["approval_id"],
            "category": action_request.payload["category"],
            "status": "pending",
            "phase": run.get("current_phase"),
            "task_id": action_request.task_id,
            "details": action_request.payload.get("details", ""),
            "created_at": now,
            "resolved_at": None,
        }
        run.setdefault("approval_state", {}).setdefault("records", []).append(approval_record)
        return

    if action == "resolve_approval":
        approval_id = action_request.payload["approval_id"]
        resolution = action_request.payload.get("resolution", "approved")
        for record in run.setdefault("approval_state", {}).setdefault("records", []):
            if record.get("approval_id") == approval_id and record.get("status") == "pending":
                record["status"] = resolution
                record["resolved_at"] = now
                break
        return

    if action == "mark_hard_stop":
        run["hard_stop"] = {
            "active": True,
            "reason_code": action_request.payload.get("reason_code", "manual_stop"),
            "details": action_request.payload.get("details", ""),
            "task_id": action_request.task_id,
            "raised_at": now,
        }
        return

    if action == "clear_hard_stop":
        run["hard_stop"] = {
            "active": False,
            "reason_code": None,
            "details": None,
            "task_id": None,
            "raised_at": None,
        }
        return

    if action == "complete_run":
        run["run_status"] = "completed"
        run["closed_at"] = now
        summary = run.setdefault("completion_summary", {})
        summary["finalized_at"] = now
        return

    if action == "abort_run":
        run["run_status"] = "aborted"
        run["closed_at"] = now
        return

    raise ValueError(f"Unsupported run action: {action}")


def _close_current_phase(
    run_ledger: dict[str, Any],
    now: str,
    *,
    exit_status: str,
    exit_reason: str,
) -> None:
    phase_history = run_ledger.setdefault("phase_history", [])
    if not phase_history:
        return
    current = phase_history[-1]
    if current.get("exited_at") is None:
        current["exited_at"] = now
        current["status"] = exit_status
        current["exit_reason"] = exit_reason


def _enter_phase(
    run_ledger: dict[str, Any],
    target_phase: str,
    now: str,
    reason: str,
    task_id: str | None,
) -> None:
    run_ledger["current_phase"] = target_phase
    run_ledger.setdefault("phase_history", []).append(
        {
            "phase": target_phase,
            "entered_at": now,
            "exited_at": None,
            "status": "entered",
            "entry_reason": reason,
            "exit_reason": "",
            "trigger_task_id": task_id,
            "transition_in_id": None,
            "transition_out_id": None,
        }
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
