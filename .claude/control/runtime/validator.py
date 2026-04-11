from __future__ import annotations

from typing import Any

from models import ActionRequest, GuardResult, LoadedState, ValidationResult

TERMINAL_RUN_STATUSES = {"completed", "aborted", "failed"}
TERMINAL_TASK_STATUSES = {"completed", "aborted", "failed", "noop"}
TASK_ACTIONS = {
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


def validate_action(request: ActionRequest, state: LoadedState) -> ValidationResult:
    run = state.run_ledger
    task = state.task_ledgers.get(request.task_id) if request.task_id else None
    guards: list[GuardResult] = []

    allowed_actions = set(run.get("allowed_next_actions", []))
    if request.action not in allowed_actions:
        guards.append(
            GuardResult(
                "policy",
                "allowed_next_actions",
                "fail",
                f"action={request.action} not in run_ledger.allowed_next_actions",
            )
        )
        return _finalize("denied", _reason_for_action_type(request.action), "Action not currently allowed.", guards)

    if run.get("run_status") in TERMINAL_RUN_STATUSES and request.action not in {"abort_run"}:
        guards.append(
            GuardResult(
                "policy",
                "run_not_terminal",
                "fail",
                f"run_status={run.get('run_status')}",
            )
        )
        return _finalize("denied", "illegal_phase_transition", "Run is already terminal.", guards)

    if request.action in TASK_ACTIONS and request.action != "create_task":
        if request.task_id is None:
            guards.append(GuardResult("policy", "task_id_required", "fail", "task action requires task_id"))
            return _finalize("denied", "illegal_task_transition", "Task action requires task_id.", guards)
        if task is None:
            guards.append(
                GuardResult(
                    "task_dependency",
                    "task_exists",
                    "fail",
                    f"task_id={request.task_id} not found",
                )
            )
            return _finalize("denied", "illegal_task_transition", "Task not found.", guards)

    if request.action == "create_task":
        return _validate_create_task(request, state, guards)
    if request.action == "start_task":
        return _validate_start_task(task, guards)
    if request.action == "move_task_to_waiting":
        return _validate_move_task_to_waiting(request, task, guards)
    if request.action == "block_task":
        return _validate_block_task(task, guards)
    if request.action == "retry_task":
        return _validate_retry_task(task, guards)
    if request.action == "complete_task":
        return _validate_complete_task(task, guards, request.payload)
    if request.action == "fail_task":
        return _validate_fail_task(task, guards)
    if request.action == "abort_task":
        return _validate_abort_task(task, guards)
    if request.action == "noop_task":
        return _validate_noop_task(task, guards)
    if request.action == "advance_phase":
        return _validate_advance_phase(request, state, guards)
    if request.action == "reroute_phase":
        return _validate_reroute_phase(request, state, guards)
    if request.action == "pause_run":
        return _validate_pause_run(state, guards)
    if request.action == "resume_run":
        return _validate_resume_run(state, guards)
    if request.action == "request_approval":
        return _validate_request_approval(request, state, guards)
    if request.action == "resolve_approval":
        return _validate_resolve_approval(request, state, guards)
    if request.action == "mark_hard_stop":
        return _validate_mark_hard_stop(state, guards)
    if request.action == "clear_hard_stop":
        return _validate_clear_hard_stop(state, guards)
    if request.action == "complete_run":
        return _validate_complete_run(state, guards)
    if request.action == "abort_run":
        return _validate_abort_run(state, guards)

    guards.append(
        GuardResult(
            "policy",
            "default_action_gate",
            "pass",
            "No additional validator beyond allowed_next_actions.",
        )
    )
    return _finalize("allowed", "other", "Action allowed by default runtime gate.", guards)


def _validate_create_task(request: ActionRequest, state: LoadedState, guards: list[GuardResult]) -> ValidationResult:
    required_keys = {"task_id", "task_group", "task_kind", "objective"}
    missing = [key for key in required_keys if key not in request.payload]
    if missing:
        guards.append(
            GuardResult(
                "task_contract",
                "create_task_payload",
                "fail",
                f"missing required payload keys: {', '.join(sorted(missing))}",
            )
        )
        return _finalize("denied", "illegal_task_transition", "create_task payload incomplete.", guards)

    task_id = str(request.payload["task_id"])
    if task_id in state.task_ledgers:
        guards.append(GuardResult("task_dependency", "task_id_unique", "fail", f"task_id already exists: {task_id}"))
        return _finalize("denied", "illegal_task_transition", "task_id already exists.", guards)

    if state.run_ledger.get("hard_stop", {}).get("active"):
        guards.append(GuardResult("hard_stop", "no_create_under_hard_stop", "fail", "hard_stop.active=true"))
        return _finalize("blocked", "hard_stop_active", "Cannot create task while hard stop is active.", guards)

    task_group = str(request.payload["task_group"])
    allowed_groups = {phase["name"] for phase in state.phase_graph.get("phases", [])}
    if task_group not in allowed_groups:
        guards.append(GuardResult("phase_graph", "task_group_known", "fail", f"task_group={task_group} not in phase graph"))
        return _finalize("denied", "illegal_task_transition", "Unknown task_group.", guards)

    guards.append(GuardResult("task_contract", "create_task_payload", "pass", "create_task payload validated"))
    return _finalize("allowed", "other", "Task creation allowed.", guards)


def _validate_start_task(task: dict[str, Any] | None, guards: list[GuardResult]) -> ValidationResult:
    assert task is not None
    if task.get("status") not in {"created", "ready"}:
        guards.append(GuardResult("policy", "task_startable", "fail", f"task.status={task.get('status')}"))
        return _finalize("denied", "illegal_task_transition", "Task is not startable.", guards)
    if task.get("blocked_by_task_ids"):
        guards.append(GuardResult("task_dependency", "task_not_blocked", "fail", "blocked_by_task_ids is not empty"))
        return _finalize("blocked", "task_dependency_blocked", "Task still has blockers.", guards)
    guards.append(GuardResult("policy", "task_startable", "pass", "task is startable"))
    return _finalize("allowed", "other", "Task start allowed.", guards)


def _validate_move_task_to_waiting(request: ActionRequest, task: dict[str, Any] | None, guards: list[GuardResult]) -> ValidationResult:
    assert task is not None
    if task.get("status") in TERMINAL_TASK_STATUSES:
        guards.append(GuardResult("policy", "task_not_terminal", "fail", f"task.status={task.get('status')}"))
        return _finalize("denied", "illegal_task_transition", "Terminal task cannot move to waiting.", guards)
    reason_code = request.payload.get("reason_code", "dependency_unfinished")
    if reason_code not in {"dependency_unfinished", "approval_required"}:
        guards.append(GuardResult("policy", "waiting_reason_code", "fail", f"unsupported reason_code={reason_code}"))
        return _finalize("denied", "illegal_task_transition", "Unsupported waiting reason.", guards)
    guards.append(GuardResult("policy", "waiting_reason_code", "pass", f"reason_code={reason_code}"))
    return _finalize("allowed", "task_dependency_blocked", "Task waiting transition allowed.", guards)


def _validate_block_task(task: dict[str, Any] | None, guards: list[GuardResult]) -> ValidationResult:
    assert task is not None
    if task.get("status") in TERMINAL_TASK_STATUSES:
        guards.append(GuardResult("policy", "task_not_terminal", "fail", f"task.status={task.get('status')}"))
        return _finalize("denied", "illegal_task_transition", "Terminal task cannot be blocked.", guards)
    guards.append(GuardResult("policy", "task_not_terminal", "pass", "task remains mutable"))
    return _finalize("allowed", "task_dependency_blocked", "Task block allowed.", guards)


def _validate_retry_task(task: dict[str, Any] | None, guards: list[GuardResult]) -> ValidationResult:
    assert task is not None
    if task.get("status") != "retryable_failure":
        guards.append(GuardResult("policy", "retryable_status_required", "fail", f"task.status={task.get('status')}"))
        return _finalize("denied", "retry_denied", "Task is not in retryable_failure state.", guards)
    guards.append(GuardResult("policy", "retryable_status_required", "pass", "task.status=retryable_failure"))
    return _finalize("allowed", "retry_allowed", "Retry allowed.", guards)


def _validate_complete_task(task: dict[str, Any] | None, guards: list[GuardResult], payload: dict[str, Any] | None = None) -> ValidationResult:
    assert task is not None
    if task.get("status") in TERMINAL_TASK_STATUSES:
        guards.append(GuardResult("policy", "task_not_terminal", "fail", f"task.status={task.get('status')}"))
        return _finalize("denied", "illegal_task_transition", "Task is already terminal.", guards)

    checks = payload.get("completion_checks") if payload and "completion_checks" in payload else task.get("completion_checks", {})
    if not checks.get("required_outputs_present", False):
        guards.append(GuardResult("task_contract", "required_outputs_present", "fail", "completion_checks.required_outputs_present=false"))
        return _finalize("blocked", "task_acceptance_contract_failed", "Required outputs are missing.", guards)
    if not checks.get("required_artifacts_present", False):
        guards.append(GuardResult("artifact_evidence", "required_artifacts_present", "fail", "completion_checks.required_artifacts_present=false"))
        return _finalize("blocked", "task_acceptance_contract_failed", "Required artifacts are missing.", guards)
    if not checks.get("validation_passed", False):
        guards.append(GuardResult("task_contract", "validation_passed", "fail", "completion_checks.validation_passed=false"))
        return _finalize("blocked", "task_acceptance_contract_failed", "Task validation has not passed.", guards)

    guards.append(GuardResult("task_contract", "task_completion_contract", "pass", "Task completion contract satisfied."))
    return _finalize("allowed", "task_acceptance_contract_met", "Task completion allowed.", guards)


def _validate_fail_task(task: dict[str, Any] | None, guards: list[GuardResult]) -> ValidationResult:
    assert task is not None
    if task.get("status") in TERMINAL_TASK_STATUSES:
        guards.append(GuardResult("policy", "task_not_terminal", "fail", f"task.status={task.get('status')}"))
        return _finalize("denied", "illegal_task_transition", "Terminal task cannot fail again.", guards)
    guards.append(GuardResult("policy", "task_not_terminal", "pass", "task remains mutable"))
    return _finalize("allowed", "other", "Task failure allowed.", guards)


def _validate_noop_task(task: dict[str, Any] | None, guards: list[GuardResult]) -> ValidationResult:
    assert task is not None
    if task.get("status") in TERMINAL_TASK_STATUSES:
        guards.append(GuardResult("policy", "task_not_terminal", "fail", f"task.status={task.get('status')}"))
        return _finalize("denied", "illegal_task_transition", "Terminal task cannot be nooped.", guards)
    guards.append(GuardResult("policy", "task_not_terminal", "pass", "task remains mutable"))
    return _finalize("noop", "noop", "noop_task accepted.", guards)


def _validate_abort_task(task: dict[str, Any] | None, guards: list[GuardResult]) -> ValidationResult:
    assert task is not None
    if task.get("status") in TERMINAL_TASK_STATUSES:
        guards.append(GuardResult("policy", "task_not_terminal", "fail", f"task.status={task.get('status')}"))
        return _finalize("denied", "illegal_task_transition", "Terminal task cannot be aborted.", guards)
    guards.append(GuardResult("policy", "task_not_terminal", "pass", "task remains mutable"))
    return _finalize("allowed", "other", "Task abort allowed.", guards)


def _validate_advance_phase(request: ActionRequest, state: LoadedState, guards: list[GuardResult]) -> ValidationResult:
    target_phase = request.payload.get("target_phase")
    allowed_next_phases = set(state.run_ledger.get("allowed_next_phases", []))
    if not target_phase:
        guards.append(GuardResult("phase_graph", "target_phase_required", "fail", "advance_phase payload missing target_phase"))
        return _finalize("denied", "illegal_phase_transition", "advance_phase requires target_phase.", guards)
    if target_phase not in allowed_next_phases:
        guards.append(GuardResult("phase_graph", "allowed_next_phases", "fail", f"target_phase={target_phase} not allowed"))
        return _finalize("denied", "illegal_phase_transition", "Target phase is not allowed.", guards)
    if not _current_phase_exit_ready(state.run_ledger):
        guards.append(GuardResult("phase_graph", "phase_exit_ready", "fail", "Current phase is not exit-ready."))
        return _finalize("blocked", "phase_exit_condition_not_met", "Current phase is not exit-ready.", guards)
    guards.append(GuardResult("phase_graph", "phase_exit_ready", "pass", f"advance to {target_phase}"))
    return _finalize("allowed", "phase_exit_condition_met", "Phase advance allowed.", guards)


def _validate_reroute_phase(request: ActionRequest, state: LoadedState, guards: list[GuardResult]) -> ValidationResult:
    target_phase = request.payload.get("target_phase")
    allowed_phase_names = {phase["name"] for phase in state.phase_graph.get("phases", [])}
    if not target_phase:
        guards.append(GuardResult("phase_graph", "target_phase_required", "fail", "reroute_phase payload missing target_phase"))
        return _finalize("denied", "illegal_phase_transition", "reroute_phase requires target_phase.", guards)
    if target_phase not in allowed_phase_names:
        guards.append(GuardResult("phase_graph", "target_phase_known", "fail", f"unknown target_phase={target_phase}"))
        return _finalize("denied", "illegal_phase_transition", "Unknown target phase.", guards)
    guards.append(GuardResult("phase_graph", "target_phase_known", "pass", f"reroute to {target_phase}"))
    return _finalize("allowed", "reroute_without_semantic_change", "Phase reroute allowed.", guards)


def _validate_pause_run(state: LoadedState, guards: list[GuardResult]) -> ValidationResult:
    if state.run_ledger.get("run_status") == "paused":
        guards.append(GuardResult("policy", "run_not_paused", "fail", "run is already paused"))
        return _finalize("noop", "noop", "Run is already paused.", guards)
    guards.append(GuardResult("policy", "run_not_paused", "pass", "run can be paused"))
    return _finalize("allowed", "other", "Run pause allowed.", guards)


def _validate_resume_run(state: LoadedState, guards: list[GuardResult]) -> ValidationResult:
    if state.run_ledger.get("run_status") != "paused":
        guards.append(GuardResult("policy", "run_is_paused", "fail", f"run_status={state.run_ledger.get('run_status')}"))
        return _finalize("denied", "illegal_phase_transition", "Run is not paused.", guards)
    guards.append(GuardResult("policy", "run_is_paused", "pass", "run can be resumed"))
    return _finalize("allowed", "other", "Run resume allowed.", guards)


def _validate_request_approval(request: ActionRequest, state: LoadedState, guards: list[GuardResult]) -> ValidationResult:
    required_keys = {"approval_id", "category"}
    missing = [key for key in required_keys if key not in request.payload]
    if missing:
        guards.append(GuardResult("approval_matrix", "request_approval_payload", "fail", f"missing keys: {', '.join(sorted(missing))}"))
        return _finalize("denied", "illegal_phase_transition", "request_approval payload incomplete.", guards)

    active_ids = set(state.run_ledger.get("approval_state", {}).get("active_approval_ids", []))
    if request.payload["approval_id"] in active_ids:
        guards.append(GuardResult("approval_matrix", "approval_id_unique", "fail", f"approval_id already active: {request.payload['approval_id']}"))
        return _finalize("denied", "illegal_phase_transition", "approval_id already active.", guards)

    guards.append(GuardResult("approval_matrix", "request_approval_payload", "pass", "approval request validated"))
    return _finalize("allowed", "scope_change_requires_approval", "Approval request allowed.", guards)


def _validate_resolve_approval(request: ActionRequest, state: LoadedState, guards: list[GuardResult]) -> ValidationResult:
    approval_id = request.payload.get("approval_id")
    active_ids = set(state.run_ledger.get("approval_state", {}).get("active_approval_ids", []))
    if not approval_id:
        guards.append(GuardResult("approval_matrix", "approval_id_required", "fail", "resolve_approval payload missing approval_id"))
        return _finalize("denied", "illegal_phase_transition", "resolve_approval requires approval_id.", guards)
    if approval_id not in active_ids:
        guards.append(GuardResult("approval_matrix", "approval_id_active", "fail", f"approval_id={approval_id} not active"))
        return _finalize("denied", "approval_rejected", "Approval is not active.", guards)
    guards.append(GuardResult("approval_matrix", "approval_id_active", "pass", f"approval_id={approval_id} active"))
    return _finalize("allowed", "approval_granted", "Approval resolution allowed.", guards)


def _validate_mark_hard_stop(state: LoadedState, guards: list[GuardResult]) -> ValidationResult:
    if state.run_ledger.get("hard_stop", {}).get("active", False):
        guards.append(GuardResult("hard_stop", "hard_stop_inactive", "fail", "hard_stop.active=true"))
        return _finalize("noop", "noop", "Hard stop is already active.", guards)
    guards.append(GuardResult("hard_stop", "hard_stop_inactive", "pass", "hard stop can be marked"))
    return _finalize("allowed", "unsafe_runtime_state", "Hard stop activation allowed.", guards)


def _validate_clear_hard_stop(state: LoadedState, guards: list[GuardResult]) -> ValidationResult:
    if not state.run_ledger.get("hard_stop", {}).get("active", False):
        guards.append(GuardResult("hard_stop", "hard_stop_active", "fail", "hard_stop.active=false"))
        return _finalize("denied", "illegal_phase_transition", "No active hard stop to clear.", guards)
    guards.append(GuardResult("hard_stop", "hard_stop_active", "pass", "hard_stop.active=true"))
    return _finalize("allowed", "other", "Hard stop can be cleared.", guards)


def _validate_complete_run(state: LoadedState, guards: list[GuardResult]) -> ValidationResult:
    summary = state.run_ledger.get("completion_summary", {})
    if not summary.get("completion_eligible", False):
        guards.append(GuardResult("phase_graph", "completion_eligible", "fail", "completion_summary.completion_eligible=false"))
        return _finalize("blocked", "phase_exit_condition_not_met", "Run is not completion-eligible.", guards)

    completion_policy = state.phase_graph.get("completion_policy", {})
    allowed_phases = set(completion_policy.get("run_may_complete_from_phases", []))
    current_phase = state.run_ledger.get("current_phase")
    if current_phase not in allowed_phases:
        guards.append(GuardResult("phase_graph", "completion_phase_allowed", "fail", f"current_phase={current_phase} not completion-eligible by policy"))
        return _finalize("denied", "illegal_phase_transition", "Current phase cannot complete the run.", guards)

    guards.append(GuardResult("phase_graph", "completion_phase_allowed", "pass", f"current_phase={current_phase}"))
    return _finalize("allowed", "phase_exit_condition_met", "Run completion allowed.", guards)


def _validate_abort_run(state: LoadedState, guards: list[GuardResult]) -> ValidationResult:
    if state.run_ledger.get("run_status") == "aborted":
        guards.append(GuardResult("policy", "run_not_aborted", "fail", "run is already aborted"))
        return _finalize("noop", "noop", "Run is already aborted.", guards)
    guards.append(GuardResult("policy", "run_not_aborted", "pass", "run can be aborted"))
    return _finalize("allowed", "other", "Run abort allowed.", guards)


def _current_phase_exit_ready(run_ledger: dict[str, Any]) -> bool:
    phase_exit = run_ledger.get("phase_exit_readiness")
    if isinstance(phase_exit, dict):
        return bool(phase_exit.get("exit_ready", False))
    return False


def _reason_for_action_type(action: str) -> str:
    return "illegal_task_transition" if action in TASK_ACTIONS else "illegal_phase_transition"


def _finalize(decision: str, reason_code: str, reason: str, guards: list[GuardResult]) -> ValidationResult:
    return ValidationResult(
        decision=decision,  # type: ignore[arg-type]
        reason_code=reason_code,
        reason=reason,
        guard_results=guards,
    )
