from __future__ import annotations

from copy import deepcopy

from apply_run_action import apply_run_action
from apply_task_action import apply_task_action
from loader import load_state
from models import ActionRequest, DispatchResult, LoadedState
from reconcile import reconcile_authoritative
from transition_builder import build_transition_record
from validator import validate_action


def dispatch_action(
    control_root: str,
    action_request: ActionRequest,
    *,
    runtime_runs_root: str | None = None,
    mode: str = "authoritative",
) -> DispatchResult:
    state = load_state(control_root, action_request.run_id, runtime_runs_root=runtime_runs_root)
    effective_task_id = _effective_task_id(action_request)
    pre_run = deepcopy(state.run_ledger)
    pre_task = deepcopy(state.task_ledgers.get(effective_task_id)) if effective_task_id else None

    validation = validate_action(action_request, state)
    working_state = deepcopy(state)

    if validation.decision in {"allowed", "noop"}:
        _apply_action(action_request, working_state)

    post_task = None
    if effective_task_id:
        post_task = working_state.task_ledgers.get(effective_task_id)

    provisional_transition = build_transition_record(
        action_request=action_request,
        validation=validation,
        pre_run_ledger=pre_run,
        pre_task_ledger=pre_task,
        post_run_ledger=working_state.run_ledger,
        post_task_ledger=post_task,
    )
    working_state.transition_records = [*working_state.transition_records, provisional_transition]

    reconcile_output = reconcile_authoritative(working_state, mode=mode)  # type: ignore[arg-type]
    final_run = reconcile_output.run_ledger

    final_transition = build_transition_record(
        action_request=action_request,
        validation=validation,
        pre_run_ledger=pre_run,
        pre_task_ledger=pre_task,
        post_run_ledger=final_run,
        post_task_ledger=post_task,
    )

    return DispatchResult(
        ok=validation.decision in {"allowed", "noop"},
        transition_id=final_transition["transition_id"],
        run_id=action_request.run_id,
        task_id=effective_task_id,
        decision=validation.decision,
        run_status=str(final_run["run_status"]),
        current_phase=str(final_run["current_phase"]),
        allowed_next_actions=list(final_run.get("allowed_next_actions", [])),
        integrity_alerts=list(reconcile_output.reconcile_result.get("integrity_alerts", [])),
        transition_record=final_transition,
        task_ledgers=working_state.task_ledgers,
        reconcile_result=reconcile_output.reconcile_result,
        run_ledger=final_run,
    )


def _effective_task_id(action_request: ActionRequest) -> str | None:
    if action_request.action == "create_task":
        task_id = action_request.payload.get("task_id")
        return str(task_id) if task_id is not None else None
    return action_request.task_id


def _apply_action(action_request: ActionRequest, state: LoadedState) -> None:
    if action_request.action in {
        "create_task",
        "start_task",
        "move_task_to_waiting",
        "block_task",
        "retry_task",
        "complete_task",
        "fail_task",
        "abort_task",
        "noop_task",
    }:
        _apply_task_action(action_request, state)
        return
    _apply_run_action(action_request, state)


def _apply_task_action(action_request: ActionRequest, state: LoadedState) -> None:
    apply_task_action(action_request, state)


def _apply_run_action(action_request: ActionRequest, state: LoadedState) -> None:
    apply_run_action(action_request, state)
