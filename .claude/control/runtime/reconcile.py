from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from models import LoadedState, ReconcileMode, ReconcileOutput

PHASES = [
    "leader_freeze",
    "l2_advisory",
    "l3_bridge",
    "l4_implement",
    "l4_execute",
    "l4_anomaly",
]

ACTIVE_TASK_STATUSES = {"in_progress"}
WAITING_TASK_STATUSES = {"waiting_on_dependency", "waiting_on_approval"}
BLOCKED_TASK_STATUSES = {"blocked"}
RETRYABLE_TASK_STATUSES = {"retryable_failure"}
OPEN_TASK_STATUSES = {
    "created",
    "ready",
    "in_progress",
    "waiting_on_dependency",
    "waiting_on_approval",
    "blocked",
    "retryable_failure",
}


def reconcile_authoritative(state: LoadedState, *, mode: ReconcileMode = "authoritative") -> ReconcileOutput:
    if mode == "authoritative" and not state.transition_records:
        raise ValueError("authoritative reconcile requires transition_records")

    run = deepcopy(state.run_ledger)
    tasks = deepcopy(state.task_ledgers)

    integrity_alerts: list[dict[str, Any]] = []
    if mode == "recovery" and not state.transition_records:
        integrity_alerts.append(_alert("other", "warn", "recovery reconcile is running without transition_records"))

    phase_names = {phase["name"] for phase in state.phase_graph.get("phases", [])}
    current_phase = str(run.get("current_phase"))
    if current_phase not in phase_names:
        integrity_alerts.append(_alert("unknown_current_phase", "error", f"current_phase={current_phase} is not in phase_graph"))

    task_index = _build_task_index(tasks)
    phase_task_index = _build_phase_task_index(tasks)
    integrity_alerts.extend(_validate_task_membership(tasks, phase_names))
    integrity_alerts.extend(_validate_dependencies(tasks))
    integrity_alerts.extend(_validate_l3_before_l4(run, tasks))

    approval_state = _derive_approval_state(run.get("approval_state", {}))
    hard_stop = deepcopy(run.get("hard_stop", {"active": False}))
    hard_stop_active = bool(hard_stop.get("active", False))

    phase_exit_readiness = _derive_phase_exit_readiness(current_phase=current_phase, task_ledgers=tasks)
    completion_summary = _derive_completion_summary(
        current_phase=current_phase,
        task_ledgers=tasks,
        approval_state=approval_state,
        hard_stop=hard_stop,
        phase_graph=state.phase_graph,
    )
    derived_run_status = _derive_run_status(
        current_run_status=str(run.get("run_status")),
        approval_state=approval_state,
        hard_stop=hard_stop,
    )
    allowed_next_phases = _derive_allowed_next_phases(
        current_phase=current_phase,
        phase_exit_readiness=phase_exit_readiness,
        phase_graph=state.phase_graph,
        hard_stop_active=hard_stop_active,
        approval_pending=approval_state["pending"],
    )
    allowed_next_actions = _derive_allowed_next_actions(
        phase_exit_readiness=phase_exit_readiness,
        completion_summary=completion_summary,
        hard_stop_active=hard_stop_active,
        approval_pending=approval_state["pending"],
    )
    followup_recommendations = _derive_followup_recommendations(
        phase_exit_readiness=phase_exit_readiness,
        completion_summary=completion_summary,
        hard_stop_active=hard_stop_active,
        approval_pending=approval_state["pending"],
        allowed_next_phases=allowed_next_phases,
    )

    reconcile_result = {
        "schema_version": "0.3.0",
        "run_id": run["run_id"],
        "mode": mode,
        "reconciled_at": _now_iso(),
        "source_summary": {
            "task_count": len(tasks),
            "transition_count": len(state.transition_records),
            "checkpoint_ref": run.get("checkpoint_ref"),
        },
        "derived_run_status": derived_run_status,
        "derived_current_phase": current_phase,
        "task_index": task_index,
        "phase_task_index": phase_task_index,
        "phase_exit_readiness": phase_exit_readiness,
        "allowed_next_actions": allowed_next_actions,
        "allowed_next_phases": allowed_next_phases,
        "completion_summary": completion_summary,
        "integrity_alerts": integrity_alerts,
        "followup_recommendations": followup_recommendations,
    }

    run["task_index"] = task_index
    run["phase_task_index"] = phase_task_index
    run["approval_state"] = approval_state
    run["hard_stop"] = hard_stop
    run["allowed_next_actions"] = allowed_next_actions
    run["allowed_next_phases"] = allowed_next_phases
    run["completion_summary"] = completion_summary
    run["run_status"] = derived_run_status
    run["phase_exit_readiness"] = phase_exit_readiness
    run["updated_at"] = reconcile_result["reconciled_at"]

    return ReconcileOutput(reconcile_result=reconcile_result, run_ledger=run)


def _build_task_index(task_ledgers: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    buckets = {
        "all_task_ids": [],
        "open_task_ids": [],
        "active_task_ids": [],
        "waiting_task_ids": [],
        "blocked_task_ids": [],
        "retryable_task_ids": [],
        "completed_task_ids": [],
        "failed_task_ids": [],
        "aborted_task_ids": [],
        "noop_task_ids": [],
        "phase_gate_task_ids": [],
    }
    for task_id, task in sorted(task_ledgers.items()):
        status = str(task.get("status"))
        buckets["all_task_ids"].append(task_id)
        if bool(task.get("phase_gate")):
            buckets["phase_gate_task_ids"].append(task_id)
        if status in OPEN_TASK_STATUSES:
            buckets["open_task_ids"].append(task_id)
        if status in ACTIVE_TASK_STATUSES:
            buckets["active_task_ids"].append(task_id)
        if status in WAITING_TASK_STATUSES:
            buckets["waiting_task_ids"].append(task_id)
        if status in BLOCKED_TASK_STATUSES:
            buckets["blocked_task_ids"].append(task_id)
        if status in RETRYABLE_TASK_STATUSES:
            buckets["retryable_task_ids"].append(task_id)
        if status == "completed":
            buckets["completed_task_ids"].append(task_id)
        if status == "failed":
            buckets["failed_task_ids"].append(task_id)
        if status == "aborted":
            buckets["aborted_task_ids"].append(task_id)
        if status == "noop":
            buckets["noop_task_ids"].append(task_id)
    return buckets


def _build_phase_task_index(task_ledgers: dict[str, dict[str, Any]]) -> dict[str, dict[str, list[str]]]:
    result = {phase: {"all_task_ids": [], "open_task_ids": [], "active_task_ids": [], "blocked_task_ids": [], "completed_task_ids": []} for phase in PHASES}
    for task_id, task in sorted(task_ledgers.items()):
        phase = str(task.get("task_group"))
        if phase not in result:
            continue
        status = str(task.get("status"))
        result[phase]["all_task_ids"].append(task_id)
        if status in OPEN_TASK_STATUSES:
            result[phase]["open_task_ids"].append(task_id)
        if status in ACTIVE_TASK_STATUSES:
            result[phase]["active_task_ids"].append(task_id)
        if status in BLOCKED_TASK_STATUSES:
            result[phase]["blocked_task_ids"].append(task_id)
        if status in {"completed", "noop"}:
            result[phase]["completed_task_ids"].append(task_id)
    return result


def _validate_task_membership(task_ledgers: dict[str, dict[str, Any]], phase_names: set[str]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for task_id, task in task_ledgers.items():
        task_group = str(task.get("task_group"))
        if task_group not in phase_names:
            alerts.append(_alert("unknown_task_group", "error", f"task_group={task_group} not in phase_graph", task_id))
    return alerts


def _validate_dependencies(task_ledgers: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    all_ids = set(task_ledgers)
    for task_id, task in task_ledgers.items():
        for dep in task.get("depends_on", []):
            if dep not in all_ids:
                alerts.append(_alert("missing_dependency_target", "error", f"depends_on references missing task_id={dep}", task_id))
    return alerts


def _validate_l3_before_l4(run_ledger: dict[str, Any], task_ledgers: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    l3_completed = any(entry.get("phase") == "l3_bridge" and entry.get("status") in {"completed", "noop"} for entry in run_ledger.get("phase_history", []))
    any_l4 = any(task.get("task_group") in {"l4_implement", "l4_execute", "l4_anomaly"} for task in task_ledgers.values())
    if any_l4 and not l3_completed:
        alerts.append(_alert("illegal_l4_without_l3", "error", "L4 work exists but l3_bridge has not completed."))
    return alerts


def _derive_approval_state(current_approval_state: dict[str, Any]) -> dict[str, Any]:
    records = deepcopy(current_approval_state.get("records", []))
    active_ids = [record["approval_id"] for record in records if record.get("status") == "pending"]
    return {"pending": bool(active_ids), "active_approval_ids": active_ids, "records": records}


def _derive_phase_exit_readiness(*, current_phase: str, task_ledgers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    phase_gate_tasks = [(task_id, task) for task_id, task in task_ledgers.items() if task.get("task_group") == current_phase and bool(task.get("phase_gate"))]
    blocking = [task_id for task_id, task in phase_gate_tasks if str(task.get("status")) not in {"completed", "noop"}]
    return {"current_phase": current_phase, "exit_ready": len(blocking) == 0, "blocking_phase_gate_task_ids": sorted(blocking)}


def _derive_completion_summary(*, current_phase: str, task_ledgers: dict[str, dict[str, Any]], approval_state: dict[str, Any], hard_stop: dict[str, Any], phase_graph: dict[str, Any]) -> dict[str, Any]:
    blocking = sorted(task_id for task_id, task in task_ledgers.items() if bool(task.get("phase_gate")) and str(task.get("status")) not in {"completed", "noop"})
    hard_stop_blocks_completion = bool(hard_stop.get("active", False))
    pending_approval_blocks_completion = bool(approval_state.get("pending", False))
    allowed_completion_phases = set(phase_graph.get("completion_policy", {}).get("run_may_complete_from_phases", []))
    completion_eligible = current_phase in allowed_completion_phases and not blocking and not pending_approval_blocks_completion and not hard_stop_blocks_completion
    return {
        "completion_eligible": completion_eligible,
        "blocking_open_required_phase_gate_task_ids": blocking,
        "pending_approval_blocks_completion": pending_approval_blocks_completion,
        "hard_stop_blocks_completion": hard_stop_blocks_completion,
        "finalized_at": _now_iso() if completion_eligible else None,
    }


def _derive_run_status(*, current_run_status: str, approval_state: dict[str, Any], hard_stop: dict[str, Any]) -> str:
    if hard_stop.get("active", False):
        return "blocked"
    if approval_state.get("pending", False):
        return "awaiting_approval"
    if current_run_status in {"completed", "aborted", "failed"}:
        return current_run_status
    return "in_progress"


def _derive_allowed_next_phases(*, current_phase: str, phase_exit_readiness: dict[str, Any], phase_graph: dict[str, Any], hard_stop_active: bool, approval_pending: bool) -> list[str]:
    if hard_stop_active or approval_pending or not phase_exit_readiness.get("exit_ready", False):
        return []
    for phase in phase_graph.get("phases", []):
        if phase.get("name") == current_phase:
            return list(phase.get("allowed_next_phases", []))
    return []


def _derive_allowed_next_actions(*, phase_exit_readiness: dict[str, Any], completion_summary: dict[str, Any], hard_stop_active: bool, approval_pending: bool) -> list[str]:
    if hard_stop_active:
        return ["clear_hard_stop", "request_approval", "resolve_approval", "abort_run"]
    if approval_pending:
        return ["resolve_approval", "abort_run"]
    if not phase_exit_readiness.get("exit_ready", False):
        return [
            "create_task",
            "retry_task",
            "complete_task",
            "noop_task",
            "fail_task",
            "abort_task",
            "request_approval",
            "pause_run",
            "abort_run",
        ]
    if completion_summary.get("completion_eligible", False):
        return ["advance_phase", "reroute_phase", "create_task", "request_approval", "pause_run", "complete_run", "abort_run"]
    return ["advance_phase", "reroute_phase", "create_task", "request_approval", "pause_run", "abort_run"]


def _derive_followup_recommendations(*, phase_exit_readiness: dict[str, Any], completion_summary: dict[str, Any], hard_stop_active: bool, approval_pending: bool, allowed_next_phases: list[str]) -> list[dict[str, Any]]:
    if hard_stop_active:
        return [{"action": "mark_hard_stop", "task_id": None, "target_phase": None, "reason": "Hard stop is active."}]
    if approval_pending:
        return [{"action": "request_approval", "task_id": None, "target_phase": None, "reason": "Pending approval must be resolved."}]
    if completion_summary.get("completion_eligible", False):
        return [{"action": "advance_phase", "task_id": None, "target_phase": None, "reason": "Run is completion-eligible."}]
    if phase_exit_readiness.get("exit_ready", False) and allowed_next_phases:
        return [{"action": "advance_phase", "task_id": None, "target_phase": allowed_next_phases[0], "reason": "Current phase is exit-ready."}]
    blocking_tasks = phase_exit_readiness.get("blocking_phase_gate_task_ids", [])
    if blocking_tasks:
        return [{"action": "retry_task", "task_id": blocking_tasks[0], "target_phase": None, "reason": "Phase exit is blocked by open phase-gate tasks."}]
    return [{"action": "none", "task_id": None, "target_phase": None, "reason": "No follow-up recommendation."}]


def _alert(alert_code: str, severity: str, details: str, task_id: str | None = None) -> dict[str, Any]:
    return {"alert_code": alert_code, "severity": severity, "task_id": task_id, "details": details}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
