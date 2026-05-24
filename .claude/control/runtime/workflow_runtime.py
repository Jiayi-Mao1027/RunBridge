from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from loader import ControlPaths, load_json_file, load_jsonl
from persist import append_jsonl, atomic_write_json, sanitize_json_value
from companion_observer import observe_workflow_event
from checkpoint_store import write_event_checkpoint
from completion_validator import completion_succeeded, validate_bridge_completion
from dispatch_contract import validate_dispatch_contract
from output_guardrails import validate_bridge_packet as guardrail_validate_bridge_packet
from output_guardrails import validate_bridge_result, validate_completion_report
from repo_runtime import ensure_repo_registered, get_repo_runtime_root, infer_repo_key_from_runs_root, repo_key_for_paths, update_active_run_registry
from retry_policy import decide_retry, load_retry_policies, packet_hash as retry_packet_hash
from runtime_event_envelope import normalize_runtime_event
from state_graph import stable_hash
from trajectory import record_guardrail_trajectory_step, record_workflow_trajectory_step


SCHEMA_VERSION = "0.4.0"
SNAPSHOT_DETAIL_LEVEL = "compact"
SNAPSHOT_TEXT_LIMIT = 700
SNAPSHOT_LIST_LIMIT = 8
SNAPSHOT_RECENT_BINDING_LIMIT = 12
ORCHESTRATION_ANOMALY_OPEN_SECONDS = 300
ORCHESTRATION_ANOMALY_STUCK_STATUSES = {
    "bridge_call_started",
    "bridge_window_opened",
    "bridge_packet_accepted",
    "team_create_completed",
    "task_create_completed",
    "task_created_recorded",
    "message_dispatch_completed",
}
EXECUTE_WATCHDOG_HEARTBEAT_GRACE_MULTIPLIER = 3

_DISPATCH_LOCK_GUARD = threading.RLock()
_DISPATCH_LOCK_COUNTS: dict[str, int] = {}


def _lock_key_for_run(paths: ControlPaths, run_id: str) -> str:
    return str((paths.run_root(run_id) / ".workflow_dispatch.lock").resolve())


def _acquire_file_lock(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release_file_lock(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _workflow_dispatch_lock(paths: ControlPaths, run_id: str):
    lock_key = _lock_key_for_run(paths, run_id)
    with _DISPATCH_LOCK_GUARD:
        count = _DISPATCH_LOCK_COUNTS.get(lock_key, 0)
        if count:
            _DISPATCH_LOCK_COUNTS[lock_key] = count + 1
            reentrant = True
        else:
            reentrant = False

    if reentrant:
        try:
            yield
        finally:
            with _DISPATCH_LOCK_GUARD:
                remaining = _DISPATCH_LOCK_COUNTS.get(lock_key, 1) - 1
                if remaining > 0:
                    _DISPATCH_LOCK_COUNTS[lock_key] = remaining
                else:
                    _DISPATCH_LOCK_COUNTS.pop(lock_key, None)
        return

    lock_path = Path(lock_key)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        _acquire_file_lock(handle)
        with _DISPATCH_LOCK_GUARD:
            _DISPATCH_LOCK_COUNTS[lock_key] = 1
        try:
            yield
        finally:
            with _DISPATCH_LOCK_GUARD:
                _DISPATCH_LOCK_COUNTS.pop(lock_key, None)
            _release_file_lock(handle)
    finally:
        handle.close()
EXECUTE_WATCHDOG_MIN_STALE_SECONDS = 300
BRIDGE_TRANSPORT_ERROR_TYPES = {
    "ClaudeTmuxTerminalError",
    "ClaudeTmuxTerminalApiError",
    "ClaudeTmuxSoftTimeoutNoProgress",
    "ClaudeTmuxTimeout",
    "TransientClaudeTmuxTransportApiError",
}
BRIDGE_NO_REPORT_ERROR_TYPES = {
    "BridgeLeaderNoReport",
    "RuntimeOwnedTeammateNoReport",
}
TRANSIENT_TRANSPORT_TEXT_MARKERS = (
    "econnreset",
    "unable to connect to api",
    "connection reset",
    "socket hang up",
    "transport error",
)
TEAMMATE_REPORT_LOSS_TEXT_MARKERS = (
    "teammate_report_missing",
    "teammate_report_missing_or_transport_failure",
    "teammate_report_collection_gap",
    "missing_teammates",
    "missing_teammate_reports",
    "missing teammate",
    "missing implementor report",
    "no usable implementor report",
    "no usable teammate report",
    "no implementation evidence",
)

RUN_TERMINAL_STATUSES = {"completed", "failed", "aborted"}
RUN_EVENT_STATUSES = {
    "run_completed": "completed",
    "run_failed": "failed",
    "run_aborted": "aborted",
}
BRIDGE_RESULT_STATUS_EVENT_KINDS = {
    "succeeded": "bridge_result_returned",
    "failed": "bridge_result_returned_with_failure",
    "partial": "bridge_result_returned_with_partial",
    "partial_or_failed": "bridge_result_returned_with_partial",
}

AGENT_TYPES = {"main-leader", "bridge-leader", "teammate", "hook", "runtime"}
AGENT_TYPE_ALIASES = {
    "leader-orchestrator": "main-leader",
}
BRIDGE_LEADER_EVENTS = {
    "bridge_window_opened",
    "bridge_packet_accepted",
    "bridge_packet_rejected",
    "team_create_started",
    "team_create_succeeded",
    "team_create_failed",
    "task_create_started",
    "task_create_succeeded",
    "task_create_failed",
    "message_dispatch_started",
    "message_dispatch_succeeded",
    "message_dispatch_failed",
    "team_executor_failed",
    "artifacts_ready",
    "partial_evidence_collected",
    "user_clarification_required",
    "blocked_for_user_clarification",
    "bridge_leader_fails_task",
    "task_failed_by_bridge_leader",
    "team_delete_started",
    "team_delete_succeeded",
    "team_delete_failed",
}
TERMINAL_LIFECYCLE_STATUSES = {
    "bridge_call_denied",
    "bridge_call_failed",
    "bridge_window_returned",
    "bridge_window_partial_returned",
    "bridge_window_failed",
    "bridge_window_orphaned",
    "bridge_window_interrupted",
    "paused_for_user_answer",
    "user_answer_received",
    "resume_same_l3_task",
    "continuation_of_previous_l3",
}
USER_ANSWER_WAIT_STATUSES = {"paused_for_user_answer"}

LIFECYCLE_TRANSITIONS: dict[str | None, dict[str, str]] = {
    None: {
        "bridge_call_intended": "bridge_call_intended",
        "pretooluse_denied_by_main_leader": "bridge_call_denied",
    },
    "bridge_call_intended": {
        "pretooluse_allowed_by_main_leader": "bridge_call_prechecked",
        "pretooluse_denied_by_main_leader": "bridge_call_denied",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "bridge_call_prechecked": {
        "call_bridge_sdk_started": "bridge_call_started",
        "call_bridge_sdk_error": "bridge_call_failed",
        "orphan_timeout_without_bridge_return": "bridge_window_orphaned",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "bridge_call_started": {
        "bridge_window_opened": "bridge_window_opened",
        "call_bridge_sdk_error": "bridge_call_failed",
        "orphan_timeout_without_bridge_return": "bridge_window_orphaned",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "bridge_window_opened": {
        "bridge_packet_accepted": "bridge_packet_accepted",
        "bridge_packet_rejected": "bridge_packet_rejected",
        "orphan_timeout_without_bridge_return": "bridge_window_orphaned",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "bridge_packet_rejected": {
        "bridge_result_returned": "bridge_window_returned",
        "bridge_result_returned_with_failure": "bridge_window_failed",
    },
    "bridge_packet_accepted": {
        "team_create_started": "team_create_started",
        "orphan_timeout_without_bridge_return": "bridge_window_orphaned",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "team_create_started": {
        "team_create_succeeded": "team_create_completed",
        "team_create_failed": "team_create_failed",
        "orphan_timeout_without_bridge_return": "bridge_window_orphaned",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "team_create_failed": {
        "bridge_result_returned": "bridge_window_failed",
        "bridge_result_returned_with_failure": "bridge_window_failed",
    },
    "team_create_completed": {
        "task_create_started": "task_create_started",
        "orphan_timeout_without_bridge_return": "bridge_window_orphaned",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "task_create_started": {
        "task_create_succeeded": "task_create_completed",
        "task_create_failed": "task_create_failed",
        "orphan_timeout_without_bridge_return": "bridge_window_orphaned",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "task_create_failed": {
        "bridge_result_returned": "bridge_window_failed",
        "bridge_result_returned_with_failure": "bridge_window_failed",
    },
    "task_create_completed": {
        "taskcreated_hook_accepted": "task_created_recorded",
        "taskcreated_hook_denied": "task_create_failed",
        "orphan_timeout_without_bridge_return": "bridge_window_orphaned",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "task_created_recorded": {
        "message_dispatch_started": "message_dispatch_started",
        "orphan_timeout_without_bridge_return": "bridge_window_orphaned",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "message_dispatch_started": {
        "message_dispatch_succeeded": "message_dispatch_completed",
        "message_dispatch_failed": "message_dispatch_failed",
        "orphan_timeout_without_bridge_return": "bridge_window_orphaned",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "message_dispatch_failed": {
        "message_dispatch_retry_started": "message_dispatch_started",
        "bridge_leader_fails_task": "task_failed",
        "bridge_result_returned": "bridge_window_failed",
        "bridge_result_returned_with_failure": "bridge_window_failed",
    },
    "message_dispatch_completed": {
        "team_idle_waiting": "team_waiting",
        "team_executor_failed": "task_failed",
        "artifacts_ready": "task_completion_started",
        "partial_evidence_collected": "bridge_window_partial_returned",
        "user_clarification_required": "blocked_for_user_clarification",
        "blocked_for_user_clarification": "blocked_for_user_clarification",
        "orphan_timeout_without_bridge_return": "bridge_window_orphaned",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "team_waiting": {
        "team_idle_waiting": "team_waiting",
        "artifacts_ready": "task_completion_started",
        "partial_evidence_collected": "bridge_window_partial_returned",
        "user_clarification_required": "blocked_for_user_clarification",
        "blocked_for_user_clarification": "blocked_for_user_clarification",
        "wait_timeout_or_process_lost": "team_wait_timeout",
        "bridge_leader_fails_task": "task_failed",
        "task_failed_by_bridge_leader": "task_failed",
        "orphan_timeout_without_heartbeat": "bridge_window_orphaned",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "blocked_for_user_clarification": {
        "bridge_result_returned_with_user_clarification_request": "paused_for_user_answer",
        "paused_for_user_answer": "paused_for_user_answer",
    },
    "paused_for_user_answer": {"user_answer_received": "user_answer_received"},
    "user_answer_received": {"resume_same_l3_task": "resume_same_l3_task"},
    "resume_same_l3_task": {"continuation_of_previous_l3": "continuation_of_previous_l3"},
    "team_wait_timeout": {
        "partial_evidence_collected": "bridge_window_partial_returned",
        "task_failed_by_bridge_leader": "task_failed",
        "bridge_result_returned": "bridge_window_partial_returned",
        "bridge_result_returned_with_partial": "bridge_window_partial_returned",
        "bridge_result_returned_with_failure": "bridge_window_failed",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "task_completion_started": {
        "completion_contract_satisfied": "task_completion_completed",
        "completion_contract_rejected": "task_completion_rejected",
        "orphan_timeout_without_bridge_return": "bridge_window_orphaned",
        "orphan_timeout_without_heartbeat": "bridge_window_orphaned",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "task_completion_rejected": {
        "continue_waiting": "team_waiting",
        "retry_artifact_collection": "task_completion_started",
        "user_clarification_required": "blocked_for_user_clarification",
        "blocked_for_user_clarification": "blocked_for_user_clarification",
        "bridge_leader_fails_task": "task_failed",
        "bridge_result_returned": "bridge_window_failed",
        "bridge_result_returned_with_failure": "bridge_window_failed",
        "orphan_timeout_without_bridge_return": "bridge_window_orphaned",
        "orphan_timeout_without_heartbeat": "bridge_window_orphaned",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "task_completion_completed": {
        "team_delete_started": "team_delete_started",
        "orphan_timeout_without_bridge_return": "bridge_window_orphaned",
        "orphan_timeout_without_heartbeat": "bridge_window_orphaned",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "task_failed": {
        "team_delete_started": "team_delete_started",
        "bridge_result_returned": "bridge_window_failed",
        "bridge_result_returned_with_failure": "bridge_window_failed",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "bridge_window_partial_returned": {"team_delete_started": "team_delete_started", "bridge_call_interrupted": "bridge_window_interrupted"},
    "team_delete_started": {
        "team_delete_succeeded": "team_delete_completed",
        "team_delete_failed": "team_delete_failed",
        "orphan_timeout_without_bridge_return": "bridge_window_orphaned",
        "orphan_timeout_without_heartbeat": "bridge_window_orphaned",
        "bridge_call_interrupted": "bridge_window_interrupted",
    },
    "team_delete_completed": {
        "bridge_result_returned": "bridge_window_returned",
        "bridge_result_returned_with_failure": "bridge_window_failed",
        "bridge_result_returned_with_partial": "bridge_window_partial_returned",
    },
    "team_delete_failed": {"bridge_result_returned_with_cleanup_required": "bridge_window_partial_returned"},
}

EVENT_TO_UPDATE_KIND = {
    "session_started": "record_session_started",
    "user_prompt_submitted": "record_user_prompt_submitted",
    "semantic_frozen": "record_semantic_frozen",
    "phase_advanced": "advance_phase",
    "route_rerouted": "reroute_phase",
    "retry_attempt_scheduled": "persist_retry_attempt_scheduled",
    "enter_anomaly": "persist_enter_anomaly",
    "bridge_call_intended": "record_bridge_call_intent",
    "pretooluse_allowed_by_main_leader": "record_bridge_call_prechecked",
    "pretooluse_denied_by_main_leader": "record_bridge_call_denied",
    "call_bridge_sdk_error": "record_bridge_call_failed",
    "bridge_call_interrupted": "persist_bridge_call_interrupted",
    "bridge_window_opened": "register_bridge_window_open",
    "bridge_packet_rejected": "persist_bridge_packet_rejected",
    "team_create_succeeded": "persist_team_created",
    "team_create_failed": "persist_team_create_failed",
    "taskcreated_hook_accepted": "persist_task_created",
    "taskcreated_hook_denied": "persist_task_create_failed",
    "task_create_failed": "persist_task_create_failed",
    "message_dispatch_succeeded": "persist_message_dispatched",
    "message_dispatch_failed": "persist_message_dispatch_failed",
    "team_executor_failed": "persist_task_failed",
    "team_idle_waiting": "persist_team_waiting",
    "wait_timeout_or_process_lost": "persist_team_wait_timeout",
    "completion_contract_satisfied": "persist_task_completed",
    "completion_contract_rejected": "persist_task_completion_rejected",
    "bridge_leader_fails_task": "persist_task_failed",
    "task_failed_by_bridge_leader": "persist_task_failed",
    "user_clarification_required": "persist_user_clarification_required",
    "blocked_for_user_clarification": "persist_user_clarification_required",
    "bridge_result_returned_with_user_clarification_request": "persist_bridge_result_returned",
    "paused_for_user_answer": "persist_paused_for_user_answer",
    "user_answer_received": "persist_user_answer_received",
    "resume_same_l3_task": "persist_l3_resume_marker",
    "continuation_of_previous_l3": "persist_l3_continuation_marker",
    "team_delete_succeeded": "persist_team_deleted",
    "team_delete_failed": "persist_team_delete_failed",
    "bridge_result_returned": "persist_bridge_result_returned",
    "bridge_result_returned_with_failure": "persist_bridge_result_returned",
    "bridge_result_returned_with_partial": "persist_bridge_result_returned",
    "bridge_result_returned_with_cleanup_required": "persist_bridge_result_returned",
    "orphan_timeout_without_bridge_return": "persist_bridge_window_orphaned",
    "orphan_timeout_without_heartbeat": "persist_bridge_window_orphaned",
    "run_completed": "persist_run_completed",
    "run_failed": "persist_run_failed",
    "run_aborted": "persist_run_aborted",
}

CONTROL_EVENTS_WITHOUT_BRIDGE_LIFECYCLE = {
    "session_started",
    "user_prompt_submitted",
    "semantic_frozen",
    "phase_advanced",
    "route_rerouted",
    "retry_attempt_scheduled",
    "enter_anomaly",
    "run_completed",
    "run_failed",
    "run_aborted",
}

FAILURE_UPDATE_KINDS = {
    "record_bridge_call_denied",
    "record_bridge_call_failed",
    "persist_bridge_packet_rejected",
    "persist_team_create_failed",
    "persist_task_create_failed",
    "persist_message_dispatch_failed",
    "persist_team_wait_timeout",
    "persist_task_completion_rejected",
    "persist_task_failed",
    "persist_team_delete_failed",
    "persist_bridge_window_orphaned",
}


@dataclass(slots=True)
class WorkflowEvent:
    run_id: str
    event_kind: str
    main_session_id: str
    event_id: str = field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:16]}")
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    sub_session_id: str | None = None
    bridge_window_id: str | None = None
    team_id: str | None = None
    task_id: str | None = None
    teammate_id: str | None = None
    agent_id: str = "unknown"
    agent_type: str = "runtime"
    tool_name: str | None = None
    tool_use_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    payload_ref: str | None = None
    parent_event_id: str | None = None
    correlation_id: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any], run_ledger: dict[str, Any] | None = None) -> "WorkflowEvent":
        run = run_ledger or {}
        run_id = str(payload.get("run_id") or run.get("run_id") or "").strip()
        event_payload = payload.get("payload")
        if not isinstance(event_payload, dict):
            event_payload = {}
        return cls(
            run_id=run_id,
            event_kind=str(payload["event_kind"]).strip(),
            main_session_id=str(payload.get("main_session_id") or run.get("main_session_id") or run_id).strip(),
            event_id=str(payload.get("event_id") or f"evt_{uuid.uuid4().hex[:16]}"),
            timestamp=str(payload.get("timestamp") or _now_iso()),
            sub_session_id=_optional_str(payload.get("sub_session_id")),
            bridge_window_id=_optional_str(payload.get("bridge_window_id")),
            team_id=_optional_str(payload.get("team_id")),
            task_id=_optional_str(payload.get("task_id")),
            teammate_id=_optional_str(payload.get("teammate_id")),
            agent_id=str(payload.get("agent_id") or payload.get("requester") or "unknown"),
            agent_type=_normalize_agent_type(payload.get("agent_type")),
            tool_name=_optional_str(payload.get("tool_name")),
            tool_use_id=_optional_str(payload.get("tool_use_id")),
            payload=event_payload,
            payload_ref=_optional_str(payload.get("payload_ref")),
            parent_event_id=_optional_str(payload.get("parent_event_id")),
            correlation_id=_optional_str(payload.get("correlation_id")),
        )

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CheckResult:
    ok: bool
    decision: str
    code: str
    reasons: list[str]
    normalized_payload: dict[str, Any]
    derived_facts: dict[str, Any]
    audit_ref: str

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class UpdateResult:
    ok: bool
    decision: str
    transition_ids: list[str]
    new_snapshot_ref: str | None
    changed_fields: list[str]
    audit_ref: str

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class NotifyResult:
    ok: bool
    notify_items: list[dict[str, Any]]
    main_leader_inbox_ref: str
    audit_ref: str

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class WorkflowDispatchResult:
    ok: bool
    run_id: str
    event_id: str
    event_kind: str
    check_result: dict[str, Any]
    update_result: dict[str, Any]
    notify_result: dict[str, Any]
    runtime_snapshot: dict[str, Any]
    written_paths: dict[str, str] = field(default_factory=dict)


def dispatch_workflow_event(
    control_root: str | Path,
    event_payload: dict[str, Any],
    *,
    runtime_runs_root: str | Path | None = None,
    repo_key: str | None = None,
    persist: bool = False,
) -> WorkflowDispatchResult:
    event_repo_key = repo_key or event_payload.get("repo_key")
    payload_obj = event_payload.get("payload") if isinstance(event_payload.get("payload"), dict) else {}
    if not event_repo_key:
        event_repo_key = payload_obj.get("repo_key")
    if event_repo_key:
        event_payload = deepcopy(event_payload)
        event_payload.setdefault("repo_key", str(event_repo_key))
        payload_copy = deepcopy(payload_obj)
        payload_copy.setdefault("repo_key", str(event_repo_key))
        event_payload["payload"] = payload_copy
        if runtime_runs_root is None:
            runtime_runs_root = get_repo_runtime_root(control_root, str(event_repo_key))
    paths = ControlPaths.from_root(control_root, runtime_runs_root)
    run_id = str(event_payload.get("run_id") or "").strip()
    if persist and run_id and not event_payload.get("_workflow_dispatch_lock_held"):
        locked_payload = deepcopy(event_payload)
        locked_payload["_workflow_dispatch_lock_held"] = True
        with _workflow_dispatch_lock(paths, run_id):
            return dispatch_workflow_event(
                control_root,
                locked_payload,
                runtime_runs_root=runtime_runs_root,
                repo_key=repo_key,
                persist=persist,
            )
    existing_run = load_json_file(paths.run_ledger_path(run_id), default={}) or {}
    event = WorkflowEvent.from_payload(event_payload, existing_run)
    if not event.run_id:
        raise ValueError("workflow event requires run_id")

    run_ledger = _ensure_run_ledger(existing_run, event)
    snapshot_before = build_runtime_snapshot(paths, run_ledger)
    lifecycle_transitions = load_lifecycle_transitions(paths)
    allowed_policy_events = load_allowed_policy_events(paths)
    check_result = check_event(event, snapshot_before, lifecycle_transitions, allowed_policy_events, control_root=paths.control_root)
    update_kind = EVENT_TO_UPDATE_KIND.get(event.event_kind, "generic_runtime_update")

    should_apply_update = check_result.decision == "allow" or _denied_failure_update_is_recordable(check_result, update_kind)
    if not should_apply_update:
        update_result = UpdateResult(
            ok=False,
            decision="rejected",
            transition_ids=[],
            new_snapshot_ref=None,
            changed_fields=[],
            audit_ref=f"upd_{uuid.uuid4().hex[:16]}",
        )
        snapshot_after = snapshot_before
    else:
        run_after, transitions = update_runtime(event, run_ledger, check_result, update_kind, lifecycle_transitions)
        snapshot_after = build_runtime_snapshot(paths, run_after)
        changed = _changed_top_level_fields(snapshot_before, snapshot_after)
        update_result = UpdateResult(
            ok=True,
            decision="applied",
            transition_ids=[t["transition_id"] for t in transitions],
            new_snapshot_ref=f"snapshot:{event.run_id}:{event.event_id}",
            changed_fields=changed,
            audit_ref=f"upd_{uuid.uuid4().hex[:16]}",
        )
        run_ledger = run_after

    auto_recovery_plan = _build_auto_recovery_plan(paths, event, check_result, snapshot_after, run_ledger)
    if auto_recovery_plan:
        check_result.derived_facts["auto_recovery"] = _compact_auto_recovery_plan(auto_recovery_plan)

    notify_result = notify(event, snapshot_after, check_result, update_result)
    written_paths: dict[str, str] = {}
    if persist:
        written_paths = persist_workflow_result(
            paths=paths,
            event=event,
            run_ledger=run_ledger,
            snapshot=snapshot_after,
            check_result=check_result,
            update_result=update_result,
            notify_result=notify_result,
        )
        if auto_recovery_plan:
            written_paths.update(_dispatch_auto_recovery_plan(control_root, auto_recovery_plan, persist=persist))

    return WorkflowDispatchResult(
        ok=check_result.ok and update_result.ok,
        run_id=event.run_id,
        event_id=event.event_id,
        event_kind=event.event_kind,
        check_result=check_result.as_record(),
        update_result=update_result.as_record(),
        notify_result=notify_result.as_record(),
        runtime_snapshot=snapshot_after,
        written_paths=written_paths,
    )


def _build_auto_recovery_plan(
    paths: ControlPaths,
    event: WorkflowEvent,
    check_result: CheckResult,
    snapshot: dict[str, Any],
    run_ledger: dict[str, Any],
) -> dict[str, Any]:
    if event.event_kind in {"retry_attempt_scheduled", "enter_anomaly"}:
        return {}
    retry_scope = _retry_scope_for_failure(event, check_result, snapshot)
    if not retry_scope:
        return {}
    packet_hash = _packet_hash_for_retry(event, run_ledger)
    attempt = _next_retry_attempt(run_ledger, event, retry_scope, packet_hash)
    error_type = _retry_error_type(event, check_result)
    reason = {
        "source_event_id": event.event_id,
        "source_event_kind": event.event_kind,
        "check_decision": check_result.decision,
        "check_reasons": list(check_result.reasons),
    }
    guardrail = check_result.derived_facts.get("guardrail_validation")
    if isinstance(guardrail, dict):
        reason["guardrail_validation"] = deepcopy(guardrail)
    decision = decide_retry(
        load_retry_policies(paths.control_root),
        retry_scope,
        attempt=attempt,
        error_type=error_type,
        reason=reason,
    )
    repo_key = str(snapshot.get("repo_key") or repo_key_for_paths(paths.control_root, paths.runtime_runs_root))
    retry_payload = decision.as_event_payload(
        repo_key=repo_key,
        run_id=event.run_id,
        bridge_window_id=event.bridge_window_id,
        packet_hash=packet_hash,
    )
    terminal_bridge_result = _is_terminal_bridge_result_event(event)
    requires_same_packet = bool(
        decision.policy.get("requires_same_packet_boundary", False)
        or retry_scope in {"bridge_sdk_call", "completion_rejected"}
        or (retry_scope == "teammate_report_missing" and terminal_bridge_result)
    )
    retry_payload.update(
        {
            "source_event_id": event.event_id,
            "source_event_kind": event.event_kind,
            "same_packet_boundary_required": requires_same_packet,
        }
    )
    if retry_scope == "teammate_report_missing" and terminal_bridge_result:
        retry_payload.update(
            {
                "retry_after_terminal_bridge_result": True,
                "source_bridge_window_id": event.bridge_window_id,
                "target_bridge_window_id_or_null": None,
                "bridge_window_reuse_allowed": False,
            }
        )
    process_poll_guard = _process_poll_retry_guard(event, retry_scope, decision.policy)
    if retry_scope == "l4_execute_process_poll" and not process_poll_guard.get("allowed", True):
        anomaly_payload = {
            **retry_payload,
            "target_phase": "l4_anomaly",
            "anomaly_reason": process_poll_guard.get("reason") or "process_poll_not_allowed",
            "process_poll_guard": process_poll_guard,
            "next_action": "enter_anomaly",
        }
        return {
            "repo_key": repo_key,
            "runtime_runs_root": str(paths.runtime_runs_root),
            "retry_decision": anomaly_payload,
            "source_event_id": event.event_id,
            "source_event_kind": event.event_kind,
            "dispatch_event": _runtime_recovery_event(event, "enter_anomaly", anomaly_payload),
            "next_action": "enter_anomaly",
        }
    plan: dict[str, Any] = {
        "repo_key": repo_key,
        "runtime_runs_root": str(paths.runtime_runs_root),
        "retry_decision": retry_payload,
        "source_event_id": event.event_id,
        "source_event_kind": event.event_kind,
        "dispatch_event": None,
        "next_action": decision.next_action,
    }
    if decision.retryable:
        retry_payload["retry_action"] = _retry_action_contract(event, retry_scope, decision, process_poll_guard)
        plan["dispatch_event"] = _runtime_recovery_event(event, "retry_attempt_scheduled", retry_payload)
    elif decision.exhausted:
        anomaly_payload = {
            **retry_payload,
            "target_phase": "l4_anomaly",
            "anomaly_reason": "retry_exhausted",
            "next_action": "enter_anomaly",
        }
        plan["dispatch_event"] = _runtime_recovery_event(event, "enter_anomaly", anomaly_payload)
        plan["next_action"] = "enter_anomaly"
    return plan


def _dispatch_auto_recovery_plan(control_root: str | Path, plan: dict[str, Any], *, persist: bool) -> dict[str, str]:
    event_payload = plan.get("dispatch_event") if isinstance(plan.get("dispatch_event"), dict) else None
    if not event_payload:
        return {"auto_recovery_next_action": str(plan.get("next_action") or "surface_non_retryable_failure")}
    try:
        result = dispatch_workflow_event(
            control_root,
            event_payload,
            runtime_runs_root=plan.get("runtime_runs_root"),
            persist=persist,
        )
    except Exception as exc:
        return {"auto_recovery_error": f"{exc.__class__.__name__}: {exc}"}
    return {
        "auto_recovery_event_kind": result.event_kind,
        "auto_recovery_event_id": result.event_id,
        "auto_recovery_ok": str(result.ok).lower(),
    }


def _compact_auto_recovery_plan(plan: dict[str, Any]) -> dict[str, Any]:
    decision = plan.get("retry_decision") if isinstance(plan.get("retry_decision"), dict) else {}
    dispatch_event = plan.get("dispatch_event") if isinstance(plan.get("dispatch_event"), dict) else {}
    return {
        "retry_scope": decision.get("retry_scope"),
        "attempt": decision.get("attempt"),
        "max_attempts": decision.get("max_attempts"),
        "retryable": decision.get("retryable"),
        "exhausted": decision.get("exhausted"),
        "delay_ms": decision.get("delay_ms"),
        "packet_hash": decision.get("packet_hash"),
        "next_action": plan.get("next_action"),
        "dispatch_event_kind": dispatch_event.get("event_kind"),
        "retry_action": decision.get("retry_action"),
    }


def _runtime_recovery_event(event: WorkflowEvent, event_kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": event.run_id,
        "main_session_id": event.main_session_id,
        "sub_session_id": event.sub_session_id,
        "bridge_window_id": event.bridge_window_id,
        "team_id": event.team_id,
        "task_id": event.task_id,
        "agent_id": "runtime.retry",
        "agent_type": "runtime",
        "tool_name": event.tool_name,
        "tool_use_id": event.tool_use_id,
        "event_kind": event_kind,
        "timestamp": _now_iso(),
        "parent_event_id": event.event_id,
        "correlation_id": event.correlation_id or event.event_id,
        "payload": payload,
    }


def _retry_scope_for_failure(event: WorkflowEvent, check_result: CheckResult, snapshot: dict[str, Any]) -> str | None:
    if event.event_kind == "completion_contract_rejected":
        return "completion_rejected"
    if event.event_kind == "wait_timeout_or_process_lost":
        payload_refs = event.payload.get("owned_process_refs") if isinstance(event.payload.get("owned_process_refs"), list) else []
        if snapshot.get("current_phase") == "l4_execute" or payload_refs:
            return "l4_execute_process_poll"
        return "teammate_report_missing"
    if event.event_kind in {"call_bridge_sdk_error", "team_create_failed", "task_create_failed", "message_dispatch_failed"}:
        return "bridge_sdk_call"
    if event.event_kind == "bridge_result_returned_with_failure":
        bridge_error_type = _bridge_result_error_type(event.payload)
        if bridge_error_type in BRIDGE_NO_REPORT_ERROR_TYPES:
            return "teammate_report_missing"
        if bridge_error_type in BRIDGE_TRANSPORT_ERROR_TYPES:
            return "bridge_sdk_call"
        if _bridge_result_reports_teammate_transport_loss(event.payload):
            return "teammate_report_missing"
    if event.event_kind in {"bridge_result_returned", "bridge_result_returned_with_partial", "bridge_result_returned_with_cleanup_required"}:
        if _bridge_result_reports_teammate_transport_loss(event.payload):
            return "teammate_report_missing"
        if "bridge_result_guardrail_failed" in check_result.reasons:
            return "completion_rejected"
    if event.event_kind == "completion_contract_satisfied" and not check_result.ok:
        if any(reason.startswith("completion_") for reason in check_result.reasons):
            return "completion_rejected"
    if "completion_report_guardrail_failed" in check_result.reasons or "bridge_result_guardrail_failed" in check_result.reasons:
        return "completion_rejected"
    if "bridge_packet_guardrail_failed" in check_result.reasons:
        return "bridge_sdk_call"
    return None


def _retry_action_contract(
    event: WorkflowEvent,
    retry_scope: str,
    decision: Any,
    process_poll_guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kind_by_scope = {
        "bridge_sdk_call": "retry_bridge_sdk_call",
        "teammate_report_missing": "continue_waiting",
        "completion_rejected": "repair_bridge_output",
        "l4_execute_process_poll": "poll_process",
    }
    terminal_bridge_result = _is_terminal_bridge_result_event(event)
    if event.event_kind == "message_dispatch_failed":
        action_kind = "retry_message_dispatch"
    elif retry_scope == "teammate_report_missing" and terminal_bridge_result:
        action_kind = "retry_bridge_sdk_call"
    else:
        action_kind = kind_by_scope.get(retry_scope, "retry_bridge_sdk_call")
    requires_same_packet = bool(
        decision.policy.get("requires_same_packet_boundary", False)
        or retry_scope in {"bridge_sdk_call", "completion_rejected"}
        or (retry_scope == "teammate_report_missing" and terminal_bridge_result)
    )
    action = {
        "kind": action_kind,
        "allowed": bool(decision.retryable),
        "requires_new_bridge_window": bool(retry_scope == "teammate_report_missing" and terminal_bridge_result),
        "requires_same_packet": requires_same_packet,
        "requires_user": False,
    }
    if retry_scope == "l4_execute_process_poll":
        action["process_poll_constraints"] = process_poll_guard or {}
    return action


def _is_terminal_bridge_result_event(event: WorkflowEvent) -> bool:
    return event.event_kind in {
        "bridge_result_returned",
        "bridge_result_returned_with_partial",
        "bridge_result_returned_with_cleanup_required",
        "bridge_result_returned_with_failure",
    }


def _process_poll_retry_guard(event: WorkflowEvent, retry_scope: str, policy: dict[str, Any]) -> dict[str, Any]:
    if retry_scope != "l4_execute_process_poll":
        return {"allowed": True}
    refs = event.payload.get("owned_process_refs") if isinstance(event.payload.get("owned_process_refs"), list) else []
    terminal_states = {"completed", "succeeded", "failed", "error", "exited", "terminated", "killed", "lost", "unknown_terminal"}
    running_states = {"running", "started", "active", "pending", "unknown"}
    ref_states = [str(ref.get("status") or ref.get("state") or "unknown").lower() for ref in refs if isinstance(ref, dict)]
    if refs and ref_states and all(state in terminal_states for state in ref_states):
        return {"allowed": False, "reason": "process_refs_terminal", "terminal_states": ref_states}
    timeout_policy = event.payload.get("timeout_policy") if isinstance(event.payload.get("timeout_policy"), dict) else {}
    hard_timeout_seconds = _positive_int_or_none(timeout_policy.get("hard_timeout_seconds"))
    hard_timeout_disabled = timeout_policy.get("executor_hard_timeout_disabled") is True
    started_at = _parse_iso(event.payload.get("process_started_at") or event.payload.get("started_at"))
    now = _parse_iso(event.timestamp) or datetime.now(timezone.utc)
    elapsed_seconds = _elapsed_seconds(started_at, now) if started_at else None
    if not hard_timeout_disabled and hard_timeout_seconds is not None and elapsed_seconds is not None and elapsed_seconds >= hard_timeout_seconds:
        return {"allowed": False, "reason": "hard_timeout_elapsed", "elapsed_seconds": elapsed_seconds, "hard_timeout_seconds": hard_timeout_seconds}
    heartbeat_timeout_ms = _positive_int_or_none(policy.get("heartbeat_timeout_ms"))
    last_heartbeat_at = _parse_iso(event.payload.get("last_heartbeat_at"))
    heartbeat_age_seconds = _elapsed_seconds(last_heartbeat_at, now) if last_heartbeat_at else None
    return {
        "allowed": True,
        "reason": "poll_allowed_until_terminal_or_hard_timeout",
        "retry_until_terminal_process_state": bool(policy.get("retry_until_terminal_process_state", True)),
        "heartbeat_timeout_ms": heartbeat_timeout_ms,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "hard_timeout_seconds": hard_timeout_seconds,
        "hard_timeout_disabled": hard_timeout_disabled,
        "observed_process_states": ref_states or sorted(running_states)[:1],
    }


def _retry_error_type(event: WorkflowEvent, check_result: CheckResult) -> str:
    guardrail = check_result.derived_facts.get("guardrail_validation")
    if isinstance(guardrail, dict) and guardrail.get("error_type"):
        return str(guardrail.get("error_type"))
    bridge_error_type = _bridge_result_error_type(event.payload)
    if bridge_error_type:
        return bridge_error_type
    error = event.payload.get("error_or_null")
    if isinstance(error, dict):
        for key in ("error_type", "type", "code"):
            if error.get(key):
                return str(error.get(key))
    if _payload_has_transient_transport_marker(event.payload):
        return "TransientClaudeTmuxTransportApiError"
    if isinstance(error, dict):
        if error.get("message"):
            return str(error.get("message")).split(":", 1)[0][:80] or "RuntimeError"
    if check_result.reasons:
        return str(check_result.reasons[0])
    return event.event_kind or "RuntimeError"


def _bridge_result_error_type(payload: dict[str, Any]) -> str | None:
    bridge_result = payload.get("bridge_result") if isinstance(payload, dict) else None
    if not isinstance(bridge_result, dict):
        return None
    error = bridge_result.get("error_or_null")
    if not isinstance(error, dict):
        return None
    for key in ("error_type", "type", "code"):
        value = error.get(key)
        if value:
            return str(value)
    return None


def _bridge_result_reports_teammate_transport_loss(payload: dict[str, Any]) -> bool:
    bridge_result = payload.get("bridge_result") if isinstance(payload, dict) else None
    if not isinstance(bridge_result, dict):
        return False
    evidence = bridge_result.get("evidence") if isinstance(bridge_result.get("evidence"), dict) else {}
    error = bridge_result.get("error_or_null") if isinstance(bridge_result.get("error_or_null"), dict) else {}
    observer_reconciliation = evidence.get("observer_reconciliation") if isinstance(evidence.get("observer_reconciliation"), dict) else {}
    if (
        evidence.get("diagnostic_classification") == "teammate_report_collection_gap"
        or error.get("diagnostic_classification") == "teammate_report_collection_gap"
        or error.get("type") == "TeammateReportCollectionGap"
    ) and observer_reconciliation.get("teammates"):
        return False
    text = _compact_text_facts(bridge_result)
    if not _contains_any_marker(text, TRANSIENT_TRANSPORT_TEXT_MARKERS):
        return False
    return _contains_any_marker(text, TEAMMATE_REPORT_LOSS_TEXT_MARKERS)


def _payload_has_transient_transport_marker(payload: Any) -> bool:
    return _contains_any_marker(_compact_text_facts(payload), TRANSIENT_TRANSPORT_TEXT_MARKERS)


def _compact_text_facts(value: Any, *, _depth: int = 0) -> str:
    if _depth > 5:
        return ""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, (int, float, bool)):
        return str(value).lower()
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            parts.append(str(key).lower())
            parts.append(_compact_text_facts(item, _depth=_depth + 1))
            if sum(len(part) for part in parts) > 20000:
                break
        return " ".join(part for part in parts if part)
    if isinstance(value, list):
        parts = []
        for item in value[:80]:
            parts.append(_compact_text_facts(item, _depth=_depth + 1))
            if sum(len(part) for part in parts) > 20000:
                break
        return " ".join(part for part in parts if part)
    return str(value).lower()


def _contains_any_marker(text: str, markers: tuple[str, ...]) -> bool:
    normalized_text = " ".join(str(text or "").lower().split())
    squashed_text = "".join(normalized_text.split())
    for marker in markers:
        normalized_marker = " ".join(str(marker or "").lower().split())
        if not normalized_marker:
            continue
        if normalized_marker in normalized_text:
            return True
        if "".join(normalized_marker.split()) in squashed_text:
            return True
    return False


def _packet_hash_for_retry(event: WorkflowEvent, run_ledger: dict[str, Any]) -> str | None:
    packet = _packet_from_event(event)
    if isinstance(packet, dict):
        return retry_packet_hash(packet)
    for record in reversed(run_ledger.get("recent_events", []) if isinstance(run_ledger.get("recent_events"), list) else []):
        if not isinstance(record, dict):
            continue
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        packet = payload.get("packet") if isinstance(payload.get("packet"), dict) else None
        if packet:
            return retry_packet_hash(packet)
    for transition in reversed(run_ledger.get("workflow_transitions", []) if isinstance(run_ledger.get("workflow_transitions"), list) else []):
        if not isinstance(transition, dict):
            continue
        payload = transition.get("payload") if isinstance(transition.get("payload"), dict) else {}
        packet = payload.get("packet") if isinstance(payload.get("packet"), dict) else None
        if packet:
            return retry_packet_hash(packet)
    return None


def _next_retry_attempt(run_ledger: dict[str, Any], event: WorkflowEvent, retry_scope: str, packet_hash: str | None) -> int:
    current_attempt = _positive_int_or_none(event.payload.get("attempt")) or 1
    latest_attempt = current_attempt
    retry_context = run_ledger.get("retry_context") if isinstance(run_ledger.get("retry_context"), dict) else {}
    attempts = retry_context.get("attempts") if isinstance(retry_context.get("attempts"), list) else []
    for attempt_record in attempts:
        if not isinstance(attempt_record, dict):
            continue
        if attempt_record.get("retry_scope") != retry_scope:
            continue
        if event.bridge_window_id and attempt_record.get("bridge_window_id") != event.bridge_window_id:
            continue
        if packet_hash and attempt_record.get("packet_hash") not in {None, packet_hash}:
            continue
        latest_attempt = max(latest_attempt, _positive_int_or_none(attempt_record.get("attempt")) or 0)
    return max(2, latest_attempt + 1)


def _positive_int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _bridge_result_event_kind_for_payload(bridge_result: dict[str, Any]) -> str | None:
    if bridge_result.get("cleanup_required") is True:
        return "bridge_result_returned_with_cleanup_required"
    status = str(bridge_result.get("status") or "").strip()
    return BRIDGE_RESULT_STATUS_EVENT_KINDS.get(status)


def check_event(
    event: WorkflowEvent,
    snapshot: dict[str, Any],
    lifecycle_transitions: dict[str | None, dict[str, str]] | None = None,
    allowed_policy_events: set[str] | None = None,
    control_root: str | Path | None = None,
) -> CheckResult:
    reasons: list[str] = []
    normalized_payload = deepcopy(event.payload)
    derived_facts: dict[str, Any] = {}
    transitions = lifecycle_transitions or LIFECYCLE_TRANSITIONS

    if not event.run_id:
        reasons.append("invalid_run_id")
    if event.agent_type not in AGENT_TYPES:
        reasons.append("unknown_agent_type")
    if event.main_session_id != snapshot.get("main_session_id"):
        reasons.append("main_session_id_not_bound_to_run")
    if allowed_policy_events is not None and event.event_kind not in allowed_policy_events and event.event_kind not in _all_known_events(transitions):
        reasons.append("event_kind_not_allowed_by_policy")

    integrity = snapshot.get("integrity", {})
    if event.event_kind in {"bridge_call_intended", "pretooluse_allowed_by_main_leader", "call_bridge_sdk_started"}:
        if integrity.get("has_hard_stop"):
            reasons.append("hard_stop_blocks_bridge_call")
        if integrity.get("awaiting_approval"):
            reasons.append("approval_pending_blocks_bridge_call")
    if event.event_kind == "bridge_call_intended" and "call_bridge_sdk" not in snapshot.get("allowed_actions", []):
        reasons.append("bridge_call_not_allowed_in_current_phase")

    if event.event_kind in BRIDGE_LEADER_EVENTS:
        if event.agent_type != "bridge-leader":
            reasons.append("only_bridge_leader_may_own_bridge_window_lifecycle")

    if event.event_kind in {"bridge_call_intended", "pretooluse_allowed_by_main_leader", "call_bridge_sdk_started"}:
        packet = normalized_payload.get("packet")
        reasons.extend(_validate_bridge_packet(packet, event, snapshot))
        guardrail = guardrail_validate_bridge_packet(packet, snapshot=snapshot, control_root=control_root)
        if not guardrail.get("valid"):
            reasons.append("bridge_packet_guardrail_failed")
            derived_facts["guardrail_validation"] = guardrail

    if event.event_kind in {
        "bridge_result_returned",
        "bridge_result_returned_with_failure",
        "bridge_result_returned_with_partial",
        "bridge_result_returned_with_cleanup_required",
    }:
        bridge_result = normalized_payload.get("bridge_result")
        if isinstance(bridge_result, dict):
            completion_contract = normalized_payload.get("completion_contract") if isinstance(normalized_payload.get("completion_contract"), dict) else None
            guardrail = validate_bridge_result(bridge_result, control_root=control_root, completion_contract=completion_contract)
            if not guardrail.get("valid"):
                reasons.append("bridge_result_guardrail_failed")
                derived_facts["guardrail_validation"] = guardrail
            expected_event_kind = _bridge_result_event_kind_for_payload(bridge_result)
            if expected_event_kind and event.event_kind != expected_event_kind:
                reasons.append("bridge_result_event_kind_status_mismatch")
                derived_facts["expected_bridge_result_event_kind"] = expected_event_kind

    if event.event_kind in CONTROL_EVENTS_WITHOUT_BRIDGE_LIFECYCLE:
        derived_facts["from_status"] = None
        derived_facts["to_status"] = event.event_kind
    else:
        bridge_status = _status_for_event(snapshot, event)
        to_status = _resolve_transition(transitions, bridge_status, event.event_kind)
        if to_status is None:
            if event.event_kind not in EVENT_TO_UPDATE_KIND and event.event_kind not in _all_known_events(transitions):
                reasons.append("unknown_event_kind")
            else:
                reasons.append("lifecycle_transition_not_allowed")
        else:
            derived_facts["from_status"] = bridge_status
            derived_facts["to_status"] = to_status

    binding = _bridge_binding(snapshot, event.bridge_window_id)
    if binding:
        if event.team_id and binding.get("team_id_or_null") not in {None, event.team_id}:
            reasons.append("bridge_window_already_has_different_team")
        if event.task_id and binding.get("task_id_or_null") not in {None, event.task_id}:
            reasons.append("bridge_window_already_has_different_task")

    if event.event_kind == "taskcreated_hook_accepted":
        reasons.extend(_validate_task_created_payload(event, normalized_payload, binding))

    if event.event_kind == "completion_contract_satisfied":
        contract = normalized_payload.get("completion_contract") or normalized_payload.get("contract") or {}
        checks = normalized_payload.get("completion_checks") or {}
        guardrail = validate_completion_report(normalized_payload, control_root=control_root)
        if not guardrail.get("valid"):
            reasons.append("completion_report_guardrail_failed")
            derived_facts["guardrail_validation"] = guardrail
        packet_for_validation, packet_ref = _completion_packet_for_window(snapshot, event, normalized_payload)
        if packet_ref:
            derived_facts["completion_packet_ref"] = packet_ref
        completion_evidence = normalized_payload.get("completion_evidence") if isinstance(normalized_payload.get("completion_evidence"), dict) else {}
        completion_execution = {
            "status": "succeeded",
            "reports": normalized_payload.get("reports", []),
            "artifact_refs": normalized_payload.get("artifact_refs", []),
            "evidence": completion_evidence,
            "error_or_null": None,
            "cleanup_required": False,
        }
        waiting = normalized_payload.get("waiting")
        if waiting is None:
            waiting = completion_evidence.get("waiting")
        if waiting is not None:
            completion_execution["waiting"] = bool(waiting)
        owned_process_refs = normalized_payload.get("owned_process_refs")
        if owned_process_refs is None:
            owned_process_refs = completion_evidence.get("owned_process_refs")
        if owned_process_refs is not None:
            completion_execution["owned_process_refs"] = owned_process_refs if isinstance(owned_process_refs, list) else []
        completion_validation = validate_bridge_completion(
            packet_for_validation,
            completion_execution,
            context={
                "run_id": event.run_id,
                "bridge_window_id": event.bridge_window_id,
                "team_id": event.team_id,
                "task_id": event.task_id,
                "agent_id": event.agent_id,
                "event_id": event.event_id,
                "timestamp": event.timestamp,
            },
            control_root=control_root,
            base_dir=_run_root_from_snapshot(snapshot),
        )
        derived_facts["completion_validation"] = completion_validation
        recorded_completion_ok = (
            isinstance(checks, dict)
            and bool(checks)
            and _completion_contract_satisfied(contract, normalized_payload, checks)
            and completion_succeeded(checks)
        )
        if not isinstance(contract, dict) or not contract:
            reasons.append("completion_contract_missing")
        elif not recorded_completion_ok and (
            not _completion_contract_satisfied(contract, normalized_payload, checks)
            or not completion_succeeded(completion_validation)
        ):
            reasons.append("completion_contract_not_satisfied")
        if not normalized_payload.get("completion_evidence") and not normalized_payload.get("reports") and not normalized_payload.get("artifact_refs"):
            reasons.append("completion_evidence_missing")

    blocking_reason_codes = {
        "invalid_run_id",
        "main_session_id_not_bound_to_run",
        "unknown_agent_type",
        "event_kind_not_allowed_by_policy",
        "hard_stop_blocks_bridge_call",
        "approval_pending_blocks_bridge_call",
        "bridge_call_not_allowed_in_current_phase",
        "only_bridge_leader_may_own_bridge_window_lifecycle",
        "bridge_packet_schema_invalid",
        "bridge_packet_must_bind_exactly_one_team_and_one_task",
        "bridge_packet_binding_mismatch",
        "bridge_packet_route_not_allowed",
        "bridge_packet_frozen_semantics_mismatch",
        "bridge_packet_frozen_scope_mismatch",
        "bridge_packet_semantic_refresh_required",
        "bridge_packet_missing_allowed_actions",
        "bridge_packet_missing_completion_contract",
        "bridge_packet_missing_report_contract",
        "bridge_packet_completion_contract_not_policy_owned",
        "bridge_packet_report_contract_not_policy_owned",
        "bridge_packet_approval_requirements_not_runtime_owned",
        "bridge_packet_expiry_not_runtime_owned",
        "bridge_packet_phase_route_not_policy_owned",
        "bridge_packet_l3_write_scope_not_policy_owned",
        "bridge_packet_implement_requires_write_authority",
        "bridge_packet_guardrail_failed",
        "bridge_result_guardrail_failed",
        "completion_report_guardrail_failed",
        "taskcreated_payload_incomplete",
        "taskcreated_team_binding_invalid",
        "taskcreated_mapping_invalid",
        "lifecycle_transition_not_allowed",
        "bridge_window_already_has_different_team",
        "bridge_window_already_has_different_task",
        "completion_contract_missing",
        "completion_contract_not_satisfied",
        "completion_evidence_missing",
    }
    if not reasons:
        decision = "allow"
        code = "ok"
        ok = True
    elif any(reason in blocking_reason_codes for reason in reasons):
        decision = "deny"
        code = "check_failed"
        ok = False
    else:
        decision = "needs_review"
        code = "check_ambiguous"
        ok = False

    return CheckResult(
        ok=ok,
        decision=decision,
        code=code,
        reasons=reasons,
        normalized_payload=normalized_payload,
        derived_facts=derived_facts,
        audit_ref=f"chk_{uuid.uuid4().hex[:16]}",
    )


def update_runtime(
    event: WorkflowEvent,
    run_ledger: dict[str, Any],
    check_result: CheckResult,
    update_kind: str,
    lifecycle_transitions: dict[str | None, dict[str, str]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    run = deepcopy(run_ledger)
    _ensure_workflow_indexes(run)
    event_record = event.as_record()
    run["updated_at"] = event.timestamp
    run["last_event_id"] = event.event_id
    if event.event_kind == "user_prompt_submitted":
        run.setdefault("semantic", {"frozen": None, "frozen_at": None, "requires_refresh": False})
        run["semantic"]["requires_refresh"] = True
    elif event.event_kind == "semantic_frozen":
        run.setdefault("semantic", {"frozen": None, "frozen_at": None, "requires_refresh": False})
        frozen = event.payload.get("frozen_semantics")
        run["semantic"]["frozen"] = deepcopy(frozen if frozen is not None else {})
        run["semantic"]["frozen_at"] = event.timestamp
        run["semantic"]["requires_refresh"] = False
    elif event.event_kind == "phase_advanced":
        target_phase = event.payload.get("target_phase")
        if target_phase:
            run["current_phase"] = str(target_phase)
    elif event.event_kind == "route_rerouted":
        run.setdefault("route", {"current_route": [], "target_phase": None, "is_stale": False, "decided_by_event_id": None})
        if "current_route" in event.payload:
            run["route"]["current_route"] = list(event.payload.get("current_route") or [])
        if "target_phase" in event.payload:
            run["route"]["target_phase"] = event.payload.get("target_phase")
        run["route"]["is_stale"] = False
        run["route"]["decided_by_event_id"] = event.event_id
    elif event.event_kind == "enter_anomaly":
        target_phase = str(event.payload.get("target_phase") or "l4_anomaly")
        run.setdefault("route", {"current_route": [], "target_phase": None, "is_stale": False, "decided_by_event_id": None})
        current_phase = str(run.get("current_phase") or "leader_freeze")
        run["route"]["current_route"] = [current_phase, target_phase] if current_phase != target_phase else [target_phase]
        run["route"]["target_phase"] = target_phase
        run["route"]["is_stale"] = False
        run["route"]["decided_by_event_id"] = event.event_id
        run["current_phase"] = target_phase
    elif event.event_kind in RUN_EVENT_STATUSES:
        run["run_status"] = RUN_EVENT_STATUSES[event.event_kind]
        run["closed_at"] = event.timestamp
    elif event.event_kind in {"pretooluse_allowed_by_main_leader", "call_bridge_sdk_started"}:
        _record_bridge_packet_route(run, event)

    to_status = check_result.derived_facts.get("to_status")
    if to_status is None:
        to_status = _resolve_transition(lifecycle_transitions or LIFECYCLE_TRANSITIONS, _status_for_run(run, event), event.event_kind)
    if to_status is None:
        to_status = event.event_kind

    transition = _build_transition(event, update_kind, check_result, to_status)
    _apply_transition_to_run(run, event, to_status, transition)
    run.setdefault("recent_events", []).append(event_record)
    run["recent_events"] = run["recent_events"][-50:]

    if event.event_kind in {
        "bridge_result_returned",
        "bridge_result_returned_with_failure",
        "bridge_result_returned_with_partial",
        "bridge_result_returned_with_cleanup_required",
        "bridge_result_returned_with_user_clarification_request",
    }:
        run["last_bridge_result"] = _build_bridge_result(event, to_status)
        _commit_bridge_result_phase(run, event, run["last_bridge_result"], to_status)

    return run, [transition]


def notify(event: WorkflowEvent, snapshot: dict[str, Any], check_result: CheckResult, update_result: UpdateResult) -> NotifyResult:
    items: list[dict[str, Any]] = []
    if check_result.decision == "deny":
        items.append(
            _notify_item(
                "blocking",
                "policy_deny",
                f"workflow event {event.event_kind} denied",
                event,
                _recommendation_for_denial(event, snapshot, check_result),
            )
        )
    elif check_result.decision == "needs_review":
        items.append(
            _notify_item(
                "warn",
                "needs_review",
                f"workflow event {event.event_kind} needs review",
                event,
                "wait_or_request_explicit_resolution",
            )
        )

    if update_result.decision == "rejected":
        items.append(
            _notify_item(
                "error",
                "update_rejected",
                f"runtime update rejected for {event.event_kind}",
                event,
                "read_runtime_snapshot_and_recover",
            )
        )

    trigger_items = {
        "pretooluse_denied_by_main_leader": ("blocking", "bridge_call_denied", _recommended_reroute_action(snapshot)),
        "call_bridge_sdk_error": ("error", "bridge_call_failed", "read_runtime_snapshot_and_decide_retry_or_report"),
        "bridge_call_interrupted": ("warn", "bridge_window_interrupted", "treat_interrupted_bridge_as_closed_then_read_snapshot_before_dispatching_next_work"),
        "bridge_packet_rejected": ("error", "bridge_packet_rejected", "rebuild_packet_from_runtime_truth_or_report_blocked"),
        "team_create_failed": ("error", "team_create_failed", "retry_bridge_window_or_report_failure"),
        "task_create_failed": ("error", "task_create_failed", "delete_team_if_created_then_rebuild_task_packet"),
        "message_dispatch_failed": ("warn", "message_dispatch_failed", "retry_send_or_fail_task_inside_same_bridge_window"),
        "team_executor_failed": ("error", "team_executor_failed", "report_failure_without_team_idle_timeout"),
        "team_idle_waiting": ("info", "team_waiting", "continue_waiting_or_poll_according_to_timeout_policy"),
        "wait_timeout_or_process_lost": ("error", "team_wait_timeout", "collect_partial_evidence_then_decide_retry_or_fail"),
        "completion_contract_rejected": ("warn", "task_completion_rejected", "continue_waiting_retry_collection_or_fail_task"),
        "retry_attempt_scheduled": ("info", "retry_attempt_scheduled", "retry_same_packet_or_repair_after_delay"),
        "enter_anomaly": ("blocking", "enter_anomaly", "route_to_l4_anomaly_from_retry_or_recovery_exhaustion"),
        "user_clarification_required": ("blocking", "blocked_for_user_clarification", "return_question_to_main_leader_and_pause_for_user_answer"),
        "blocked_for_user_clarification": ("blocking", "blocked_for_user_clarification", "return_question_to_main_leader_and_pause_for_user_answer"),
        "bridge_result_returned_with_user_clarification_request": ("blocking", "paused_for_user_answer", "ask_user_for_clarification_then_record_user_answer_received"),
        "paused_for_user_answer": ("blocking", "paused_for_user_answer", "ask_user_for_clarification_then_record_user_answer_received"),
        "user_answer_received": ("info", "user_answer_received", "reroute_to_l3_bridge_or_leader_freeze_then_resume_same_l3_task"),
        "resume_same_l3_task": ("info", "resume_same_l3_task", "build_next_l3_bridge_packet_as_continuation"),
        "continuation_of_previous_l3": ("info", "continuation_of_previous_l3", "continue_l3_bridge_work_from_user_answer_context"),
        "team_delete_failed": ("warn", "team_delete_failed", "mark_cleanup_required_and_schedule_cleanup_followup"),
        "orphan_timeout_without_bridge_return": ("blocking", "bridge_window_orphaned", "recover_or_mark_failed_before_dispatching_new_dependent_work"),
        "orphan_timeout_without_heartbeat": ("blocking", "bridge_window_orphaned", "recover_or_mark_failed_before_dispatching_new_dependent_work"),
    }
    if event.event_kind in trigger_items:
        level, category, action = trigger_items[event.event_kind]
        items.append(_notify_item(level, category, f"workflow event {event.event_kind}", event, action))

    integrity = snapshot.get("integrity", {})
    if integrity.get("has_hard_stop"):
        items.append(_notify_item("blocking", "hard_stop", "runtime is in hard_stop state", event, "do_not_dispatch_any_new_bridge"))
    if integrity.get("awaiting_approval"):
        items.append(_notify_item("blocking", "approval_pending", "approval pending blocks next scoped action", event, "pause_execution_until_approval_resolved"))
    if integrity.get("awaiting_user_answer"):
        items.append(_notify_item("blocking", "user_answer_pending", "user clarification answer is required before the next bridge call", event, "ask_user_then_dispatch_user_answer_received"))
    if snapshot.get("route", {}).get("is_stale"):
        items.append(_notify_item("warn", "route_stale", "route view is stale", event, "recompute_possible_phase_route"))

    items = _dedupe_notify_items(items)
    return NotifyResult(
        ok=True,
        notify_items=items,
        main_leader_inbox_ref=f"inbox:{event.run_id}:{event.event_id}",
        audit_ref=f"ntf_{uuid.uuid4().hex[:16]}",
    )


def build_runtime_snapshot(paths: ControlPaths, run_ledger: dict[str, Any]) -> dict[str, Any]:
    run = deepcopy(run_ledger)
    _ensure_workflow_indexes(run)

    phase_graph = load_json_file(paths.phase_graph_path(), default={}) or {}
    current_phase = str(run.get("current_phase") or "leader_freeze")
    allowed_routes = _allowed_routes_for_phase(phase_graph, current_phase)
    lifecycle = _derive_lifecycle(run)
    snapshot_refs = _snapshot_refs(paths, run["run_id"])
    observer_summary = _observer_summary(paths, run["run_id"])
    runtime_diagnostics = _derive_runtime_diagnostics(run, lifecycle, snapshot_refs, observer_summary)
    integrity = _derive_integrity(run, runtime_diagnostics)
    phase_exit_readiness = _derive_phase_exit_readiness(run)

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_detail_level": SNAPSHOT_DETAIL_LEVEL,
        "snapshot_policy": {
            "purpose": "control_state_only",
            "large_payloads_live_in_refs": True,
            "text_preview_limit": SNAPSHOT_TEXT_LIMIT,
            "list_preview_limit": SNAPSHOT_LIST_LIMIT,
        },
        "snapshot_refs": snapshot_refs,
        "repo_key": run.get("repo_key") or repo_key_for_paths(paths.control_root, paths.runtime_runs_root),
        "run_id": run["run_id"],
        "run_status": _snapshot_run_status(run),
        "main_session_id": run.get("main_session_id") or run["run_id"],
        "current_phase": current_phase,
        "semantic": run.get("semantic", {"frozen": None, "frozen_at": None, "requires_refresh": False}),
        "scope": run.get("scope", {"frozen": None, "frozen_at": None, "requires_refresh": False}),
        "route": run.get(
            "route",
            {
                "current_route": [],
                "target_phase": None,
                "is_stale": False,
                "decided_by_event_id": None,
            },
        ),
        "lifecycle": lifecycle,
        "bindings": _compact_bindings_for_snapshot(run.get("bindings", _empty_bindings()), lifecycle),
        "allowed_actions": _derive_allowed_actions(integrity, lifecycle),
        "allowed_routes": allowed_routes,
        "integrity": integrity,
        "runtime_diagnostics": runtime_diagnostics,
        "last_bridge_result": _compact_bridge_result_for_snapshot(run.get("last_bridge_result"), snapshot_refs),
        "phase_exit_readiness": phase_exit_readiness,
    }
    return snapshot


def _snapshot_run_status(run: dict[str, Any]) -> str:
    status = str(run.get("run_status") or "in_progress").strip() or "in_progress"
    return status


def _registry_status_for_run(run: dict[str, Any]) -> str:
    status = _snapshot_run_status(run)
    if status in RUN_TERMINAL_STATUSES:
        return status
    if status in {"idle"}:
        return status
    return "running"


def persist_workflow_result(
    *,
    paths: ControlPaths,
    event: WorkflowEvent,
    run_ledger: dict[str, Any],
    snapshot: dict[str, Any],
    check_result: CheckResult,
    update_result: UpdateResult,
    notify_result: NotifyResult,
) -> dict[str, str]:
    run_root = paths.run_root(event.run_id)
    repo_root = (
        event.payload.get("repo_root")
        or event.payload.get("project_root")
        or event.payload.get("cwd")
        or event.payload.get("repo_cwd")
    )
    repo_key = infer_repo_key_from_runs_root(paths.runtime_runs_root) or snapshot.get("repo_key")
    registry_paths: dict[str, str] = {}
    try:
        registry_status = _registry_status_for_run(run_ledger)
        if repo_root:
            manifest = ensure_repo_registered(paths.control_root, repo_root, run_id=event.run_id, status=registry_status)
            registry_paths["repo_manifest"] = str(paths.control_root.parent / "runtime_state" / "projects" / manifest.repo_key / "repo_manifest.json")
        elif repo_key:
            update_active_run_registry(
                paths.control_root,
                repo_key=str(repo_key),
                repo_root=None,
                run_id=event.run_id,
                status=registry_status,
            )
        registry_paths["repo_registry"] = str(paths.control_root.parent / "runtime_state" / "registry" / "repos.json")
        registry_paths["active_runs_registry"] = str(paths.control_root.parent / "runtime_state" / "registry" / "active_runs.json")
    except Exception as exc:
        registry_paths["repo_registry_error"] = f"{exc.__class__.__name__}: {exc}"
    event_path = run_root / "event_log.jsonl"
    check_path = run_root / "check_ledger.jsonl"
    update_path = run_root / "update_ledger.jsonl"
    notify_path = run_root / "main_leader_inbox.jsonl"
    snapshot_path = run_root / "runtime_snapshot.json"

    event_sequence = _next_jsonl_sequence(event_path)
    event_record = event.as_record()
    event_record["runtime_event"] = normalize_runtime_event(
        event,
        source="runtime",
        authority="authoritative",
        seq=event_sequence,
        phase=snapshot.get("current_phase"),
        payload_ref=f"event_log.jsonl:{event.event_id}",
        safe_preview=event.event_kind,
    )
    append_jsonl(event_path, event_record)
    append_jsonl(
        check_path,
        {
            **check_result.as_record(),
            "event_id": event.event_id,
            "event_kind": event.event_kind,
            "timestamp": event.timestamp,
        },
    )
    append_jsonl(
        update_path,
        {
            **update_result.as_record(),
            "event_id": event.event_id,
            "event_kind": event.event_kind,
            "timestamp": event.timestamp,
        },
    )
    for item in notify_result.notify_items:
        append_jsonl(notify_path, {"event_id": event.event_id, "timestamp": event.timestamp, **item})
    applied_transition_ids = set(update_result.transition_ids)
    for transition in run_ledger.get("workflow_transitions", [])[-1:]:
        if transition.get("transition_id") in applied_transition_ids:
            append_jsonl(paths.transitions_path(event.run_id), transition)

    atomic_write_json(paths.run_ledger_path(event.run_id), run_ledger)
    atomic_write_json(snapshot_path, snapshot)
    try:
        companion_paths = observe_workflow_event(paths, _event_for_projection(event, check_result), snapshot)
    except Exception as exc:
        companion_paths = {"companion_observer_error": f"{exc.__class__.__name__}: {exc}"}
    try:
        checkpoint_paths = write_event_checkpoint(paths, event, snapshot)
    except Exception as exc:
        checkpoint_paths = {"checkpoint_error": f"{exc.__class__.__name__}: {exc}"}
    try:
        trajectory_paths = record_workflow_trajectory_step(paths, event, snapshot)
    except Exception as exc:
        trajectory_paths = {"trajectory_error": f"{exc.__class__.__name__}: {exc}"}
    guardrail_paths: dict[str, str] = {}
    validation = check_result.derived_facts.get("guardrail_validation")
    if isinstance(validation, dict) and not validation.get("valid"):
        try:
            guardrail_paths = record_guardrail_trajectory_step(paths, event.run_id, validation, event_ref=f"check_ledger.jsonl:{event.event_id}")
        except Exception as exc:
            guardrail_paths = {"guardrail_trajectory_error": f"{exc.__class__.__name__}: {exc}"}
    return {
        "event_log": str(event_path),
        "check_ledger": str(check_path),
        "update_ledger": str(update_path),
        "main_leader_inbox": str(notify_path),
        "runtime_snapshot": str(snapshot_path),
        "run_ledger": str(paths.run_ledger_path(event.run_id)),
        "transitions": str(paths.transitions_path(event.run_id)),
        **registry_paths,
        **companion_paths,
        **checkpoint_paths,
        **trajectory_paths,
        **guardrail_paths,
    }


def load_recent_workflow_events(paths: ControlPaths, run_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    records = load_jsonl(paths.run_root(run_id) / "event_log.jsonl")
    return records[-limit:]


def _next_jsonl_sequence(path: Path) -> int:
    if not path.exists():
        return 1
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) + 1
    except Exception:
        return 1


def reconcile_workflow_from_ledger(
    control_root: str | Path,
    run_id: str,
    *,
    repo_key: str | None = None,
    runtime_runs_root: str | Path | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    if repo_key and runtime_runs_root is None:
        runtime_runs_root = get_repo_runtime_root(control_root, repo_key)
    paths = ControlPaths.from_root(control_root, runtime_runs_root)
    current_run = load_json_file(paths.run_ledger_path(run_id), default={}) or {}
    event_records = load_jsonl(paths.run_root(run_id) / "event_log.jsonl")
    if not event_records:
        base = _base_run_for_replay(current_run, run_id)
        snapshot = build_runtime_snapshot(paths, base)
        result = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "mode": "ledger_replay",
            "reconciled_at": _now_iso(),
            "source_summary": {"event_count": 0, "transition_count": 0},
            "runtime_snapshot": snapshot,
            "integrity_alerts": [
                {
                    "level": "warn",
                    "category": "empty_event_ledger",
                    "message": "event_log.jsonl is empty; snapshot was derived from run_ledger only",
                    "related_ids": {"run_id": run_id},
                }
            ],
        }
        if persist:
            _persist_reconcile_replay(paths, run_id, base, snapshot, result)
        return result

    replay_run = _base_run_for_replay(current_run, run_id, first_event=event_records[0])
    lifecycle_transitions = load_lifecycle_transitions(paths)
    allowed_policy_events = load_allowed_policy_events(paths)
    replayed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for raw_event in event_records:
        event = WorkflowEvent.from_payload(raw_event, replay_run)
        snapshot_before = build_runtime_snapshot(paths, replay_run)
        check_result = check_event(event, snapshot_before, lifecycle_transitions, allowed_policy_events, control_root=paths.control_root)
        update_kind = EVENT_TO_UPDATE_KIND.get(event.event_kind, "generic_runtime_update")
        should_apply = check_result.decision == "allow" or _denied_failure_update_is_recordable(check_result, update_kind)
        if should_apply:
            replay_run, transitions = update_runtime(event, replay_run, check_result, update_kind, lifecycle_transitions)
        else:
            transitions = []
            replay_run.setdefault("recent_events", []).append(event.as_record())
            replay_run["recent_events"] = replay_run["recent_events"][-50:]
            rejected.append(
                {
                    "event_id": event.event_id,
                    "event_kind": event.event_kind,
                    "decision": check_result.decision,
                    "reasons": check_result.reasons,
                }
            )
        replayed.append(
            {
                "event_id": event.event_id,
                "event_kind": event.event_kind,
                "decision": check_result.decision,
                "transition_ids": [transition["transition_id"] for transition in transitions],
            }
        )

    snapshot = build_runtime_snapshot(paths, replay_run)
    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "mode": "ledger_replay",
        "reconciled_at": _now_iso(),
        "source_summary": {
            "event_count": len(event_records),
            "transition_count": len(replay_run.get("workflow_transitions", [])),
            "rejected_event_count": len(rejected),
        },
        "replayed_events": replayed,
        "rejected_events": rejected,
        "runtime_snapshot": snapshot,
        "integrity_alerts": snapshot.get("integrity", {}).get("open_alerts", []),
    }
    replay_run["updated_at"] = result["reconciled_at"]
    if persist:
        _persist_reconcile_replay(paths, run_id, replay_run, snapshot, result)
    return result


def load_lifecycle_transitions(paths: ControlPaths) -> dict[str | None, dict[str, str]]:
    table_path = paths.control_root / "policy" / "lifecycle_transition_table.json"
    payload = load_json_file(table_path, default={}) or {}
    transitions = payload.get("transitions", [])
    if not isinstance(transitions, list):
        return LIFECYCLE_TRANSITIONS
    result: dict[str | None, dict[str, str]] = {}
    for item in transitions:
        if not isinstance(item, list) or len(item) != 3:
            continue
        from_status_raw, event_kind, to_status = item
        from_status = None if from_status_raw in {None, "null"} else str(from_status_raw)
        result.setdefault(from_status, {})[str(event_kind)] = str(to_status)
    return result or LIFECYCLE_TRANSITIONS


def _denied_failure_update_is_recordable(check_result: CheckResult, update_kind: str) -> bool:
    if update_kind not in FAILURE_UPDATE_KINDS:
        return False
    if check_result.decision == "allow":
        return True
    if "lifecycle_transition_not_allowed" in check_result.reasons:
        return False
    return True


def load_allowed_policy_events(paths: ControlPaths) -> set[str]:
    matrix = load_json_file(paths.approval_matrix_path(), default={}) or {}
    categories = matrix.get("categories", {})
    allowed: set[str] = set()
    if isinstance(categories, dict):
        for category in categories.values():
            if isinstance(category, dict):
                allowed.update(str(item) for item in category.get("allowed_event_kinds", []) if item)
    return allowed


def _ensure_run_ledger(run: dict[str, Any], event: WorkflowEvent) -> dict[str, Any]:
    now = event.timestamp
    if not run:
        run = {
            "schema_version": SCHEMA_VERSION,
            "run_id": event.run_id,
            "main_session_id": event.main_session_id,
            "workflow_name": "bridge_window_workflow",
            "workflow_version": SCHEMA_VERSION,
            "run_status": "in_progress",
            "current_phase": "leader_freeze",
            "created_at": now,
            "updated_at": now,
            "closed_at": None,
        }
    run.setdefault("schema_version", SCHEMA_VERSION)
    if event.payload.get("repo_key"):
        run.setdefault("repo_key", event.payload.get("repo_key"))
    run.setdefault("main_session_id", event.main_session_id)
    run.setdefault("workflow_name", "bridge_window_workflow")
    run.setdefault("workflow_version", SCHEMA_VERSION)
    run.setdefault("run_status", "in_progress")
    run.setdefault("current_phase", "leader_freeze")
    run.setdefault("created_at", now)
    run.setdefault("updated_at", now)
    run.setdefault("approval_state", {"pending": False, "active_approval_ids": [], "records": []})
    run.setdefault("hard_stop", {"active": False, "reason_code": None, "details": None, "task_id": None, "raised_at": None})
    run.setdefault("semantic", {"frozen": None, "frozen_at": None, "requires_refresh": False})
    run.setdefault("scope", {"frozen": None, "frozen_at": None, "requires_refresh": False})
    run.setdefault("route", {"current_route": [], "target_phase": None, "is_stale": False, "decided_by_event_id": None})
    _ensure_workflow_indexes(run)
    return run


def _ensure_workflow_indexes(run: dict[str, Any]) -> None:
    run.setdefault("bindings", _empty_bindings())
    bindings = run["bindings"]
    bindings.setdefault("bridge_windows", {})
    bindings.setdefault("teams", {})
    bindings.setdefault("tasks", {})
    bindings.setdefault("tool_uses", {})
    run.setdefault("lifecycle", {"status_index": {}, "last_event_index": {}, "open_bridge_window_ids": [], "orphan_candidate_ids": []})
    lifecycle = run["lifecycle"]
    lifecycle.setdefault("status_index", {})
    lifecycle.setdefault("last_event_index", {})
    lifecycle.setdefault("open_bridge_window_ids", [])
    lifecycle.setdefault("orphan_candidate_ids", [])
    run.setdefault("workflow_transitions", [])
    run.setdefault("recent_events", [])


def _apply_transition_to_run(run: dict[str, Any], event: WorkflowEvent, to_status: str, transition: dict[str, Any]) -> None:
    _ensure_workflow_indexes(run)
    if event.event_kind == "retry_attempt_scheduled":
        run.setdefault("retry_context", {"attempts": []})
        attempts = run["retry_context"].setdefault("attempts", [])
        attempts.append(deepcopy(event.payload))
        run["retry_context"]["latest"] = deepcopy(event.payload)
    bridge_window_id = None if event.event_kind in CONTROL_EVENTS_WITHOUT_BRIDGE_LIFECYCLE else event.bridge_window_id
    if bridge_window_id:
        binding = run["bindings"]["bridge_windows"].setdefault(
            bridge_window_id,
            _new_bridge_binding(event),
        )
        binding["updated_at"] = event.timestamp
        binding["lifecycle_status"] = to_status
        packet = _packet_from_event(event)
        if packet:
            binding["packet_ref"] = f"event_log.jsonl:{event.event_id}"
            binding["packet_hash"] = stable_hash(packet)
            if isinstance(packet.get("target_phase"), str):
                binding["target_phase"] = packet.get("target_phase")
        if event.sub_session_id:
            binding["sub_session_id"] = event.sub_session_id
        if event.team_id:
            binding["team_id_or_null"] = event.team_id
        if event.task_id:
            binding["task_id_or_null"] = event.task_id
        if event.agent_type == "bridge-leader":
            binding["bridge_leader_id_or_null"] = event.agent_id
        if to_status in TERMINAL_LIFECYCLE_STATUSES:
            binding["closed_at"] = event.timestamp

        run["lifecycle"]["status_index"][bridge_window_id] = to_status
        run["lifecycle"]["last_event_index"][bridge_window_id] = event.event_id
        open_ids = set(run["lifecycle"].get("open_bridge_window_ids", []))
        if to_status in TERMINAL_LIFECYCLE_STATUSES:
            open_ids.discard(bridge_window_id)
        else:
            open_ids.add(bridge_window_id)
        run["lifecycle"]["open_bridge_window_ids"] = sorted(open_ids)

    if event.team_id:
        team_binding = run["bindings"]["teams"].setdefault(
            event.team_id,
            {
                "run_id": event.run_id,
                "sub_session_id": event.sub_session_id,
                "bridge_window_id": event.bridge_window_id,
                "team_id": event.team_id,
                "team_name": event.team_id,
                "teammate_ids": [],
                "owner_agent_id": event.agent_id,
                "owner_agent_type": "bridge-leader",
            },
        )
        if event.payload.get("team_name"):
            team_binding["team_name"] = event.payload["team_name"]
        if isinstance(event.payload.get("teammate_ids"), list):
            team_binding["teammate_ids"] = event.payload["teammate_ids"]
        if event.agent_type == "bridge-leader":
            team_binding["owner_agent_id"] = event.agent_id

    if event.task_id:
        task_binding = run["bindings"]["tasks"].setdefault(
            event.task_id,
            {
                "run_id": event.run_id,
                "sub_session_id": event.sub_session_id,
                "bridge_window_id": event.bridge_window_id,
                "team_id": event.team_id,
                "task_id": event.task_id,
                "owner_agent_id": event.agent_id,
                "owner_agent_type": "bridge-leader" if event.agent_type == "bridge-leader" else event.agent_type,
            },
        )
        if event.team_id:
            task_binding["team_id"] = event.team_id
        if event.agent_type == "bridge-leader":
            task_binding["owner_agent_id"] = event.agent_id
            task_binding["owner_agent_type"] = "bridge-leader"

    if event.tool_use_id:
        run["bindings"]["tool_uses"][event.tool_use_id] = {
            "run_id": event.run_id,
            "sub_session_id": event.sub_session_id,
            "bridge_window_id": event.bridge_window_id,
            "team_id": event.team_id,
            "task_id": event.task_id,
            "tool_use_id": event.tool_use_id,
            "tool_name": event.tool_name,
            "agent_id": event.agent_id,
            "agent_type": event.agent_type,
        }

    run["workflow_transitions"].append(transition)


def _new_bridge_binding(event: WorkflowEvent) -> dict[str, Any]:
    return {
        "run_id": event.run_id,
        "main_session_id": event.main_session_id,
        "sub_session_id": event.sub_session_id,
        "bridge_window_id": event.bridge_window_id,
        "parent_tool_use_id": event.tool_use_id,
        "opened_by_agent_id": event.agent_id,
        "opened_by_agent_type": "main-leader" if event.agent_type == "main-leader" else event.agent_type,
        "bridge_leader_id_or_null": None,
        "team_id_or_null": event.team_id,
        "task_id_or_null": event.task_id,
        "lifecycle_status": "bridge_call_intended",
        "created_at": event.timestamp,
        "updated_at": event.timestamp,
        "closed_at": None,
    }


def _build_transition(event: WorkflowEvent, update_kind: str, check_result: CheckResult, to_status: str) -> dict[str, Any]:
    transition = {
        "schema_version": SCHEMA_VERSION,
        "transition_id": f"wtr_{uuid.uuid4().hex[:16]}",
        "type": to_status,
        "run_id": event.run_id,
        "main_session_id": event.main_session_id,
        "sub_session_id": event.sub_session_id,
        "bridge_window_id": event.bridge_window_id,
        "team_id": event.team_id,
        "task_id": event.task_id,
        "from_status": check_result.derived_facts.get("from_status"),
        "to_status": to_status,
        "based_on_event": event.event_id,
        "event_kind": event.event_kind,
        "update_kind": update_kind,
        "decision": check_result.decision,
        "payload": deepcopy(check_result.normalized_payload),
        "timestamp": event.timestamp,
    }
    transition["runtime_event"] = normalize_runtime_event(
        event,
        source="runtime",
        authority="authoritative",
        phase=check_result.derived_facts.get("target_phase"),
        payload_ref=f"transitions.jsonl:{transition['transition_id']}",
        safe_preview=f"{event.event_kind}->{to_status}",
    )
    return transition


def _packet_from_event(event: WorkflowEvent) -> dict[str, Any] | None:
    packet = event.payload.get("packet")
    if isinstance(packet, dict):
        return packet
    tool_input = event.payload.get("tool_input")
    if isinstance(tool_input, dict) and isinstance(tool_input.get("packet"), dict):
        return tool_input["packet"]
    return None


def _completion_packet_for_window(
    snapshot: dict[str, Any],
    event: WorkflowEvent,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    packet = payload.get("packet") if isinstance(payload.get("packet"), dict) else None
    if packet:
        return packet, payload.get("packet_ref") if isinstance(payload.get("packet_ref"), str) else "completion_payload:packet"
    packet, packet_ref = _load_packet_for_window(snapshot, event.bridge_window_id)
    if packet:
        return packet, packet_ref
    contract = payload.get("completion_contract") or payload.get("contract") or {}
    return (
        {
            "completion_contract": contract if isinstance(contract, dict) else {},
            "task_spec": payload.get("task_spec") if isinstance(payload.get("task_spec"), dict) else {},
            "report_contract": payload.get("report_contract") if isinstance(payload.get("report_contract"), dict) else {},
            "target_phase": _target_phase_for_window(snapshot, event.bridge_window_id) or snapshot.get("current_phase"),
        },
        None,
    )


def _load_packet_for_window(snapshot: dict[str, Any], bridge_window_id: str | None) -> tuple[dict[str, Any] | None, str | None]:
    if not bridge_window_id:
        return None, None
    event_log_ref = _snapshot_ref_path(snapshot, "canonical_event_log") or _snapshot_ref_path(snapshot, "event_log")
    if not event_log_ref:
        return None, None
    records = load_jsonl(Path(event_log_ref))
    for record in reversed(records):
        if str(record.get("bridge_window_id") or "") != str(bridge_window_id):
            continue
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        packet = payload.get("packet")
        if isinstance(packet, dict):
            return packet, f"event_log.jsonl:{record.get('event_id') or '?'}"
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        packet = tool_input.get("packet")
        if isinstance(packet, dict):
            return packet, f"event_log.jsonl:{record.get('event_id') or '?'}"
    return None, None


def _target_phase_for_window(snapshot: dict[str, Any], bridge_window_id: str | None) -> str | None:
    binding = _bridge_binding(snapshot, bridge_window_id)
    if isinstance(binding, dict) and binding.get("target_phase"):
        return str(binding.get("target_phase"))
    route = snapshot.get("route") if isinstance(snapshot.get("route"), dict) else {}
    if route.get("target_phase"):
        return str(route.get("target_phase"))
    return None


def _run_root_from_snapshot(snapshot: dict[str, Any]) -> str | None:
    return _snapshot_ref_path(snapshot, "run_root")


def _snapshot_ref_path(snapshot: dict[str, Any], key: str) -> str | None:
    refs = snapshot.get("snapshot_refs") if isinstance(snapshot.get("snapshot_refs"), dict) else {}
    value = refs.get(key)
    return str(value) if value else None


def _event_for_projection(event: WorkflowEvent, check_result: CheckResult) -> WorkflowEvent:
    validation = check_result.derived_facts.get("completion_validation")
    if event.event_kind not in {"completion_contract_satisfied", "completion_contract_rejected"} or not isinstance(validation, dict):
        return event
    projected = deepcopy(event)
    projected.payload = deepcopy(event.payload)
    projected.payload["completion_checks"] = validation
    projected.payload.setdefault("completion_validation", validation)
    if check_result.derived_facts.get("completion_packet_ref"):
        projected.payload.setdefault("packet_ref", check_result.derived_facts["completion_packet_ref"])
    return projected


def _record_bridge_packet_route(run: dict[str, Any], event: WorkflowEvent) -> None:
    packet = _packet_from_event(event)
    if not packet:
        return
    route = packet.get("phase_route")
    target_phase = packet.get("target_phase")
    route_state = run.setdefault(
        "route",
        {"current_route": [], "target_phase": None, "is_stale": False, "decided_by_event_id": None},
    )
    if isinstance(route, list):
        route_state["current_route"] = [str(item) for item in route]
        if target_phase is None and route:
            target_phase = route[-1]
    if target_phase is not None:
        route_state["target_phase"] = str(target_phase)
    route_state["is_stale"] = False
    route_state["decided_by_event_id"] = event.event_id


def _commit_bridge_result_phase(run: dict[str, Any], event: WorkflowEvent, bridge_result: dict[str, Any], to_status: str) -> None:
    if event.event_kind != "bridge_result_returned" or to_status != "bridge_window_returned":
        return
    if str(bridge_result.get("status") or "").lower() not in {"succeeded", "success"}:
        return
    route_state = run.get("route", {})
    target_phase = route_state.get("target_phase")
    if not target_phase:
        return
    current_route = route_state.get("current_route")
    if isinstance(current_route, list) and current_route and str(current_route[-1]) != str(target_phase):
        return
    run["current_phase"] = str(target_phase)
    route_state["is_stale"] = False


def _build_bridge_result(event: WorkflowEvent, to_status: str) -> dict[str, Any]:
    bridge_result = event.payload.get("bridge_result")
    if isinstance(bridge_result, dict):
        result = deepcopy(bridge_result)
    else:
        result = {}
    status = result.get("status")
    if not status:
        if to_status == "bridge_window_partial_returned":
            status = "partial"
        elif to_status == "bridge_window_failed":
            status = "failed"
        elif to_status == "bridge_window_orphaned":
            status = "orphaned"
        elif to_status == "paused_for_user_answer":
            status = "needs_user_answer"
        else:
            status = "succeeded"
    result.update(
        {
            "run_id": event.run_id,
            "main_session_id": event.main_session_id,
            "sub_session_id": event.sub_session_id,
            "bridge_window_id": event.bridge_window_id,
            "team_id_or_null": event.team_id,
            "task_id_or_null": event.task_id,
            "status": status,
            "returned_at": event.timestamp,
        }
    )
    result.setdefault("reports", event.payload.get("reports", []))
    result.setdefault("artifact_refs", event.payload.get("artifact_refs", []))
    result.setdefault("evidence", event.payload.get("evidence"))
    result.setdefault("error_or_null", event.payload.get("error_or_null"))
    result.setdefault("cleanup_required", event.event_kind == "bridge_result_returned_with_cleanup_required")
    return result


def _snapshot_refs(paths: ControlPaths, run_id: str) -> dict[str, str]:
    run_root = paths.run_root(run_id)
    return {
        "run_ledger": str(run_root / "run_ledger.json"),
        "event_log": str(run_root / "event_log.jsonl"),
        "canonical_event_log": str(run_root / "event_log.jsonl"),
        "transitions": str(run_root / "transitions.jsonl"),
        "main_leader_inbox": str(run_root / "main_leader_inbox.jsonl"),
        "teammate_reports": str(run_root / "teammate_reports.jsonl"),
        "tool_events": str(run_root / "tool_events.jsonl"),
        "artifacts": str(run_root / "artifacts.jsonl"),
        "process_events": str(run_root / "process_events.jsonl"),
        "completion_checks": str(run_root / "completion_checks.jsonl"),
        "active_operations": str(run_root / "active_operations.json"),
        "bridge_prompts_dir": str(run_root.parent.parent / "bridge_prompts" / run_id),
        "run_root": str(run_root),
    }


def _observer_summary(paths: ControlPaths, run_id: str) -> dict[str, Any]:
    run_root = paths.run_root(run_id)
    streams = {
        "teammate_reports": run_root / "teammate_reports.jsonl",
        "artifacts": run_root / "artifacts.jsonl",
        "process_events": run_root / "process_events.jsonl",
        "completion_checks": run_root / "completion_checks.jsonl",
        "tool_events": run_root / "tool_events.jsonl",
        "agent_messages": run_root / "agent_messages.jsonl",
    }
    by_window: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    for kind, path in streams.items():
        try:
            records = load_jsonl(path) or []
        except Exception:
            records = []
        totals[kind] = len(records)
        for record in records:
            if not isinstance(record, dict):
                continue
            bridge_window_id = str(record.get("bridge_window_id") or "").strip()
            if not bridge_window_id:
                continue
            by_window.setdefault(bridge_window_id, {})
            by_window[bridge_window_id][kind] = by_window[bridge_window_id].get(kind, 0) + 1
    return {"totals": totals, "by_bridge_window_id": by_window}


def _derive_runtime_diagnostics(
    run: dict[str, Any],
    lifecycle: dict[str, Any],
    refs: dict[str, str],
    observer_summary: dict[str, Any],
) -> dict[str, Any]:
    anomalies: list[dict[str, Any]] = []
    watchdog_alerts: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    status_index = lifecycle.get("status_index") if isinstance(lifecycle.get("status_index"), dict) else {}
    open_window_ids = [str(item) for item in lifecycle.get("open_bridge_window_ids", []) if str(item)]
    observer_by_window = observer_summary.get("by_bridge_window_id") if isinstance(observer_summary.get("by_bridge_window_id"), dict) else {}
    for bridge_window_id in open_window_ids:
        status = str(status_index.get(bridge_window_id) or "")
        binding = run.get("bindings", {}).get("bridge_windows", {}).get(bridge_window_id, {})
        if not isinstance(binding, dict):
            binding = {}
        latest_transition = _latest_transition_for_window(run, bridge_window_id)
        created_at = _parse_iso(binding.get("created_at")) or _parse_iso(latest_transition.get("timestamp"))
        last_event_at = _parse_iso(latest_transition.get("timestamp")) or created_at
        open_seconds = _elapsed_seconds(created_at, now)
        last_event_age_seconds = _elapsed_seconds(last_event_at, now)
        counts = observer_by_window.get(bridge_window_id) if isinstance(observer_by_window.get(bridge_window_id), dict) else {}
        process_count = int(counts.get("process_events", 0) or 0)
        report_count = int(counts.get("teammate_reports", 0) or 0)
        artifact_count = int(counts.get("artifacts", 0) or 0)
        completion_count = int(counts.get("completion_checks", 0) or 0)
        no_downstream_evidence = process_count == 0 and report_count == 0 and artifact_count == 0 and completion_count == 0
        stuck_after_dispatch = status == "message_dispatch_completed" and no_downstream_evidence
        open_too_long = open_seconds is not None and open_seconds >= ORCHESTRATION_ANOMALY_OPEN_SECONDS
        if status in ORCHESTRATION_ANOMALY_STUCK_STATUSES and open_too_long and no_downstream_evidence:
            conditions = ["bridge_window_open_too_long", "no_process_refs", "no_reports", "no_artifacts"]
            if stuck_after_dispatch:
                conditions.append("status_stuck_at_message_dispatch_completed")
            anomalies.append(
                {
                    "level": "blocking",
                    "category": "workflow_instability",
                    "classification": "bridge_orchestration_hang",
                    "message": "bridge window is open too long without process, report, artifact, or completion evidence",
                    "bridge_window_id": bridge_window_id,
                    "status": status,
                    "conditions": conditions,
                    "open_seconds": open_seconds,
                    "last_event_age_seconds": last_event_age_seconds,
                    "last_event_id": latest_transition.get("based_on_event"),
                    "team_id_or_null": binding.get("team_id_or_null"),
                    "task_id_or_null": binding.get("task_id_or_null"),
                    "observer_counts": {
                        "process_events": process_count,
                        "teammate_reports": report_count,
                        "artifacts": artifact_count,
                        "completion_checks": completion_count,
                    },
                    "diagnostic_refs": {
                        "runtime_snapshot": refs.get("run_root"),
                        "event_log": refs.get("event_log"),
                        "transitions": refs.get("transitions"),
                        "teammate_reports": refs.get("teammate_reports"),
                        "artifacts": refs.get("artifacts"),
                        "process_events": refs.get("process_events"),
                        "tool_events": refs.get("tool_events"),
                        "active_operations": refs.get("active_operations"),
                        "bridge_prompts_dir": refs.get("bridge_prompts_dir"),
                    },
                    "diagnostic_checklist": [
                        "do_not_continue_waiting",
                        "classify_as_workflow_instability_bridge_orchestration_hang",
                        "inspect_runtime_snapshot",
                        "inspect_event_log_and_artifact_refs",
                        "inspect_known_output_dirs",
                        "inspect_known_logs",
                        "tell_user_bridge_orchestration_is_hung_before_retry_or_reroute",
                    ],
                    "recommended_action_or_null": "do_not_wait; mark_bridge_orphaned_or_reroute_l4_anomaly_after_snapshot_event_log_artifact_process_checks",
                }
            )
        if status == "team_waiting":
            wait_payload = latest_transition.get("payload") if isinstance(latest_transition.get("payload"), dict) else {}
            process_refs = wait_payload.get("owned_process_refs") if isinstance(wait_payload.get("owned_process_refs"), list) else []
            timeout_policy = wait_payload.get("timeout_policy") if isinstance(wait_payload.get("timeout_policy"), dict) else {}
            heartbeat_seconds = _positive_int(timeout_policy.get("heartbeat_interval_seconds"), default=60)
            stale_after = max(heartbeat_seconds * EXECUTE_WATCHDOG_HEARTBEAT_GRACE_MULTIPLIER, EXECUTE_WATCHDOG_MIN_STALE_SECONDS)
            last_heartbeat_at = _parse_iso(wait_payload.get("last_heartbeat_at")) or last_event_at
            heartbeat_age_seconds = _elapsed_seconds(last_heartbeat_at, now)
            running_refs = [ref for ref in process_refs if _process_ref_looks_running(ref)]
            if running_refs and heartbeat_age_seconds is not None and heartbeat_age_seconds >= stale_after:
                watchdog_alerts.append(
                    {
                        "level": "warn",
                        "category": "execute_watchdog",
                        "classification": "execute_stale_heartbeat_with_owned_process_refs",
                        "message": "owned process refs exist but bridge heartbeat is stale; inspect process/log/artifact state instead of waiting for hard timeout",
                        "bridge_window_id": bridge_window_id,
                        "status": status,
                        "heartbeat_age_seconds": heartbeat_age_seconds,
                        "heartbeat_interval_seconds": heartbeat_seconds,
                        "stale_after_seconds": stale_after,
                        "owned_process_ref_count": len(process_refs),
                        "running_owned_process_ref_count": len(running_refs),
                        "last_event_id": latest_transition.get("based_on_event"),
                        "diagnostic_refs": {
                            "event_log": refs.get("event_log"),
                            "transitions": refs.get("transitions"),
                            "process_events": refs.get("process_events"),
                            "tool_events": refs.get("tool_events"),
                            "active_operations": refs.get("active_operations"),
                            "known_logs_ref": refs.get("tool_events"),
                            "known_outputs_ref": refs.get("artifacts"),
                        },
                        "diagnostic_checklist": [
                            "do_not_wait_until_hard_timeout_blindly",
                            "inspect_owned_process_refs",
                            "inspect_process_events_and_active_operations",
                            "inspect_known_logs",
                            "inspect_known_output_dirs",
                            "ask_or_trigger_bridge_poll_heartbeat_before_declaring_failure",
                            "emit_team_idle_or_wait_timeout_or_process_lost_based_on_evidence",
                        ],
                        "recommended_action_or_null": "run_watchdog_probe_or_route_to_l4_anomaly_if_process_status_cannot_be_confirmed",
                    }
                )
    return {
        "detail_level": "compact",
        "observer_stream_counts": observer_summary.get("totals", {}),
        "orchestration_anomalies": anomalies[:SNAPSHOT_LIST_LIMIT],
        "execute_watchdog_alerts": watchdog_alerts[:SNAPSHOT_LIST_LIMIT],
        "has_blocking_orchestration_anomaly": any(item.get("level") == "blocking" for item in anomalies),
        "has_execute_watchdog_alert": bool(watchdog_alerts),
        "omitted": {
            "orchestration_anomalies": max(0, len(anomalies) - SNAPSHOT_LIST_LIMIT),
            "execute_watchdog_alerts": max(0, len(watchdog_alerts) - SNAPSHOT_LIST_LIMIT),
        },
    }


def _compact_bridge_result_for_snapshot(result: Any, refs: dict[str, str]) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    reports = result.get("reports") if isinstance(result.get("reports"), list) else []
    artifacts = result.get("artifact_refs") if isinstance(result.get("artifact_refs"), list) else []
    evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else result.get("evidence")
    error = result.get("error_or_null")
    return {
        "detail_level": "compact",
        "full_result_ref": refs.get("run_ledger"),
        "report_stream_ref": refs.get("teammate_reports"),
        "artifact_stream_ref": refs.get("artifacts"),
        "status": result.get("status"),
        "failure_stage_or_null": result.get("failure_stage_or_null"),
        "run_id": result.get("run_id"),
        "main_session_id": result.get("main_session_id"),
        "sub_session_id": result.get("sub_session_id"),
        "bridge_window_id": result.get("bridge_window_id"),
        "team_id_or_null": result.get("team_id_or_null"),
        "task_id_or_null": result.get("task_id_or_null"),
        "returned_at": result.get("returned_at"),
        "cleanup_required": bool(result.get("cleanup_required", False)),
        "report_count": len(reports),
        "artifact_count": len(artifacts),
        "reports_preview": [_compact_value_for_snapshot(item) for item in reports[:SNAPSHOT_LIST_LIMIT]],
        "artifact_refs_preview": [_compact_artifact_ref_for_snapshot(item) for item in artifacts[:SNAPSHOT_LIST_LIMIT]],
        "evidence_summary": _compact_value_for_snapshot(evidence),
        "error_or_null": _compact_value_for_snapshot(error),
        "omitted": {
            "reports": max(0, len(reports) - SNAPSHOT_LIST_LIMIT),
            "artifact_refs": max(0, len(artifacts) - SNAPSHOT_LIST_LIMIT),
            "full_evidence": _value_size(evidence) > SNAPSHOT_TEXT_LIMIT,
        },
    }


def _compact_bindings_for_snapshot(bindings: Any, lifecycle: dict[str, Any]) -> dict[str, Any]:
    source = bindings if isinstance(bindings, dict) else _empty_bindings()
    open_windows = set(str(item) for item in lifecycle.get("open_bridge_window_ids", []) if item)
    bridge_windows = source.get("bridge_windows") if isinstance(source.get("bridge_windows"), dict) else {}
    teams = source.get("teams") if isinstance(source.get("teams"), dict) else {}
    tasks = source.get("tasks") if isinstance(source.get("tasks"), dict) else {}
    tool_uses = source.get("tool_uses") if isinstance(source.get("tool_uses"), dict) else {}

    kept_window_ids = set(open_windows)
    kept_window_ids.update(_latest_mapping_keys(bridge_windows, SNAPSHOT_RECENT_BINDING_LIMIT))
    kept_team_ids = {
        str(binding.get("team_id_or_null"))
        for window_id, binding in bridge_windows.items()
        if str(window_id) in kept_window_ids and isinstance(binding, dict) and binding.get("team_id_or_null")
    }
    kept_task_ids = {
        str(binding.get("task_id_or_null"))
        for window_id, binding in bridge_windows.items()
        if str(window_id) in kept_window_ids and isinstance(binding, dict) and binding.get("task_id_or_null")
    }
    compact_tool_uses = {
        key: _compact_value_for_snapshot(value)
        for key, value in _latest_mapping_items(tool_uses, SNAPSHOT_RECENT_BINDING_LIMIT)
    }
    return {
        "detail_level": "compact",
        "counts": {
            "bridge_windows": len(bridge_windows),
            "teams": len(teams),
            "tasks": len(tasks),
            "tool_uses": len(tool_uses),
        },
        "bridge_windows": {
            key: _compact_value_for_snapshot(value)
            for key, value in bridge_windows.items()
            if str(key) in kept_window_ids
        },
        "teams": {
            key: _compact_value_for_snapshot(value)
            for key, value in teams.items()
            if str(key) in kept_team_ids or str(value.get("bridge_window_id") if isinstance(value, dict) else "") in kept_window_ids
        },
        "tasks": {
            key: _compact_value_for_snapshot(value)
            for key, value in tasks.items()
            if str(key) in kept_task_ids or str(value.get("bridge_window_id") if isinstance(value, dict) else "") in kept_window_ids
        },
        "tool_uses": compact_tool_uses,
        "omitted": {
            "bridge_windows": max(0, len(bridge_windows) - len(kept_window_ids)),
            "tool_uses": max(0, len(tool_uses) - len(compact_tool_uses)),
        },
    }


def _latest_mapping_keys(mapping: dict[str, Any], limit: int) -> list[str]:
    return [str(key) for key in list(mapping.keys())[-limit:]]


def _latest_mapping_items(mapping: dict[str, Any], limit: int) -> list[tuple[str, Any]]:
    return [(str(key), value) for key, value in list(mapping.items())[-limit:]]


def _compact_value_for_snapshot(value: Any, *, depth: int = 0) -> Any:
    if depth > 3:
        return _compact_text(str(value))
    if isinstance(value, str):
        return _compact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_compact_value_for_snapshot(item, depth=depth + 1) for item in value[:SNAPSHOT_LIST_LIMIT]]
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for key, nested in list(value.items())[:SNAPSHOT_LIST_LIMIT]:
            compact[str(key)] = _compact_value_for_snapshot(nested, depth=depth + 1)
        omitted_keys = max(0, len(value) - SNAPSHOT_LIST_LIMIT)
        if omitted_keys:
            compact["_omitted_keys"] = omitted_keys
        return compact
    return _compact_text(str(sanitize_json_value(value)))


def _compact_artifact_ref_for_snapshot(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            "id": value.get("id"),
            "ref_type": value.get("ref_type"),
            "path": _compact_text(str(value.get("path") or "")) if value.get("path") else None,
            "sha256": value.get("sha256"),
            "safe_preview": _compact_text(str(value.get("safe_preview") or value.get("id") or "")),
        }
    return _compact_text(str(value))


def _compact_text(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= SNAPSHOT_TEXT_LIMIT:
        return cleaned
    return cleaned[:SNAPSHOT_TEXT_LIMIT] + f"... <truncated {len(cleaned) - SNAPSHOT_TEXT_LIMIT} chars>"


def _value_size(value: Any) -> int:
    try:
        import json

        return len(json.dumps(sanitize_json_value(value), ensure_ascii=False, default=str))
    except Exception:
        return len(str(value))


def _latest_transition_for_window(run: dict[str, Any], bridge_window_id: str) -> dict[str, Any]:
    transitions = run.get("workflow_transitions")
    if not isinstance(transitions, list):
        return {}
    for transition in reversed(transitions):
        if isinstance(transition, dict) and str(transition.get("bridge_window_id") or "") == bridge_window_id:
            return transition
    return {}


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _elapsed_seconds(start: datetime | None, end: datetime) -> int | None:
    if start is None:
        return None
    return max(0, int((end - start).total_seconds()))


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    if parsed <= 0:
        return default
    return parsed


def _process_ref_looks_running(ref: Any) -> bool:
    if not isinstance(ref, dict):
        return False
    status = str(ref.get("status") or ref.get("state") or "").strip().casefold()
    if status in {"running", "started", "active", "polling", "unknown"}:
        return True
    if status in {"completed", "complete", "succeeded", "failed", "error", "exited", "terminated", "killed", "cancelled", "canceled"}:
        return False
    return bool(ref.get("pid") or ref.get("process_ref") or ref.get("process_id"))


def _derive_integrity(run: dict[str, Any], runtime_diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
    approval = run.get("approval_state", {})
    hard_stop = run.get("hard_stop", {})
    alerts = []
    for bridge_window_id in run.get("lifecycle", {}).get("orphan_candidate_ids", []):
        alerts.append(
            {
                "level": "blocking",
                "category": "orphan_candidate",
                "message": "bridge window may be orphaned",
                "related_ids": {"bridge_window_id": bridge_window_id},
            }
        )
    status_index = run.get("lifecycle", {}).get("status_index", {})
    awaiting_user_answer_ids = sorted(
        bridge_window_id
        for bridge_window_id, status in status_index.items()
        if status in USER_ANSWER_WAIT_STATUSES
    )
    for bridge_window_id in awaiting_user_answer_ids:
        alerts.append(
            {
                "level": "blocking",
                "category": "user_answer_pending",
                "message": "bridge window is paused for user clarification",
                "related_ids": {"bridge_window_id": bridge_window_id},
            }
        )
    diagnostics = runtime_diagnostics if isinstance(runtime_diagnostics, dict) else {}
    for anomaly in diagnostics.get("orchestration_anomalies", []) if isinstance(diagnostics.get("orchestration_anomalies"), list) else []:
        if not isinstance(anomaly, dict):
            continue
        if anomaly.get("level") != "blocking":
            continue
        alerts.append(
            {
                "level": "blocking",
                "category": anomaly.get("classification") or "bridge_orchestration_hang",
                "message": anomaly.get("message") or "bridge orchestration anomaly detected",
                "related_ids": {"bridge_window_id": anomaly.get("bridge_window_id")},
                "recommended_action_or_null": anomaly.get("recommended_action_or_null"),
            }
        )
    for alert in diagnostics.get("execute_watchdog_alerts", []) if isinstance(diagnostics.get("execute_watchdog_alerts"), list) else []:
        if not isinstance(alert, dict):
            continue
        alerts.append(
            {
                "level": alert.get("level") or "warn",
                "category": alert.get("classification") or "execute_watchdog",
                "message": alert.get("message") or "execute watchdog alert detected",
                "related_ids": {"bridge_window_id": alert.get("bridge_window_id")},
                "recommended_action_or_null": alert.get("recommended_action_or_null"),
            }
        )
    return {
        "has_hard_stop": bool(hard_stop.get("active", False)),
        "awaiting_approval": bool(approval.get("pending", False)),
        "awaiting_user_answer": bool(awaiting_user_answer_ids),
        "awaiting_user_answer_bridge_window_ids": awaiting_user_answer_ids,
        "has_blocking_orchestration_anomaly": bool(diagnostics.get("has_blocking_orchestration_anomaly", False)),
        "has_execute_watchdog_alert": bool(diagnostics.get("has_execute_watchdog_alert", False)),
        "open_alerts": alerts,
    }


def _derive_lifecycle(run: dict[str, Any]) -> dict[str, Any]:
    lifecycle = deepcopy(run.get("lifecycle", {}))
    lifecycle.setdefault("status_index", {})
    lifecycle.setdefault("last_event_index", {})
    lifecycle.setdefault("open_bridge_window_ids", [])
    lifecycle.setdefault("orphan_candidate_ids", [])
    return lifecycle


def _derive_phase_exit_readiness(run: dict[str, Any]) -> dict[str, Any]:
    lifecycle = _derive_lifecycle(run)
    blocking = list(lifecycle.get("open_bridge_window_ids", []))
    return {
        "current_phase": run.get("current_phase", "leader_freeze"),
        "exit_ready": len(blocking) == 0,
        "blocking_event_ids": [],
        "blocking_task_ids": [],
        "blocking_bridge_window_ids": blocking,
        "changed_recently": bool(run.get("last_event_id")),
    }


def _derive_allowed_actions(integrity: dict[str, Any], lifecycle: dict[str, Any]) -> list[str]:
    if integrity.get("has_hard_stop"):
        return ["clear_hard_stop", "abort_run"]
    if integrity.get("awaiting_approval"):
        return ["resolve_approval", "abort_run"]
    if integrity.get("awaiting_user_answer"):
        return ["record_user_answer", "abort_run"]
    if lifecycle.get("open_bridge_window_ids"):
        return ["record_bridge_event", "mark_bridge_orphaned", "abort_run"]
    return ["call_bridge_sdk", "record_bridge_event", "advance_phase", "reroute_phase", "request_approval", "abort_run"]


def _allowed_routes_for_phase(phase_graph: dict[str, Any], current_phase: str) -> list[str]:
    for phase in phase_graph.get("phases", []):
        if phase.get("name") == current_phase:
            return list(phase.get("allowed_next_phases", []))
    return []


def _status_for_event(snapshot: dict[str, Any], event: WorkflowEvent) -> str | None:
    if event.bridge_window_id:
        return snapshot.get("lifecycle", {}).get("status_index", {}).get(event.bridge_window_id)
    return None


def _status_for_run(run: dict[str, Any], event: WorkflowEvent) -> str | None:
    if event.bridge_window_id:
        return run.get("lifecycle", {}).get("status_index", {}).get(event.bridge_window_id)
    return None


def _bridge_binding(snapshot: dict[str, Any], bridge_window_id: str | None) -> dict[str, Any] | None:
    if not bridge_window_id:
        return None
    binding = snapshot.get("bindings", {}).get("bridge_windows", {}).get(bridge_window_id)
    return binding if isinstance(binding, dict) else None


def _resolve_transition(transitions: dict[str | None, dict[str, str]], from_status: str | None, event_kind: str) -> str | None:
    return transitions.get(from_status, {}).get(event_kind)


def _all_known_events(transitions: dict[str | None, dict[str, str]]) -> set[str]:
    events = set(EVENT_TO_UPDATE_KIND)
    for table in transitions.values():
        events.update(table)
    return events


def _validate_bridge_packet(packet: Any, event: WorkflowEvent, snapshot: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not isinstance(packet, dict):
        return ["bridge_packet_schema_invalid"]
    if snapshot.get("semantic", {}).get("requires_refresh"):
        reasons.append("bridge_packet_semantic_refresh_required")
    binding = packet.get("binding")
    team_spec = packet.get("team_spec")
    task_spec = packet.get("task_spec")
    mapping = packet.get("task_team_mapping")
    if not all(isinstance(part, dict) for part in [binding, team_spec, task_spec, mapping]):
        reasons.append("bridge_packet_schema_invalid")
        return reasons
    team_present = bool(team_spec.get("team_name") or team_spec.get("team_id_or_null") or mapping.get("team_id_or_null"))
    task_present = bool(task_spec.get("task_subject") and task_spec.get("task_kind"))
    assignments = mapping.get("teammate_assignments", [])
    teammate_specs = team_spec.get("teammate_specs", [])
    if (
        not team_present
        or not task_present
        or not isinstance(assignments, list)
        or len(assignments) == 0
        or not isinstance(teammate_specs, list)
        or len(teammate_specs) == 0
    ):
        reasons.append("bridge_packet_must_bind_exactly_one_team_and_one_task")
    if binding.get("run_id") not in {None, event.run_id}:
        reasons.append("bridge_packet_binding_mismatch")
    expected_repo_key = event.payload.get("repo_key") or snapshot.get("repo_key")
    if expected_repo_key:
        if packet.get("repo_key") not in {None, expected_repo_key}:
            reasons.append("bridge_packet_binding_mismatch")
        if binding.get("repo_key") not in {None, expected_repo_key}:
            reasons.append("bridge_packet_binding_mismatch")
    if binding.get("main_session_id") not in {None, event.main_session_id, snapshot.get("main_session_id")}:
        reasons.append("bridge_packet_binding_mismatch")
    if not event.bridge_window_id or binding.get("bridge_window_id") != event.bridge_window_id:
        reasons.append("bridge_packet_binding_mismatch")
    if not event.sub_session_id or binding.get("sub_session_id") != event.sub_session_id:
        reasons.append("bridge_packet_binding_mismatch")
    allowed_routes = set(snapshot.get("allowed_routes", []))
    target_phase = packet.get("target_phase")
    if (
        target_phase
        and allowed_routes
        and target_phase not in allowed_routes
        and target_phase != snapshot.get("current_phase")
        and not _packet_target_matches_explicit_reroute(snapshot, str(target_phase))
    ):
        reasons.append("bridge_packet_route_not_allowed")
    allowed_actions = packet.get("allowed_actions", [])
    required_window_actions = {"team_create", "task_create", "send_messages", "task_complete", "team_delete"}
    if not isinstance(allowed_actions, list) or not required_window_actions.issubset(set(allowed_actions)):
        reasons.append("bridge_packet_missing_allowed_actions")
    snapshot_semantics = snapshot.get("semantic", {}).get("frozen")
    if snapshot_semantics is not None and packet.get("frozen_semantics") != snapshot_semantics:
        reasons.append("bridge_packet_frozen_semantics_mismatch")
    snapshot_scope = snapshot.get("scope", {}).get("frozen")
    if snapshot_scope is not None and packet.get("frozen_scope") != snapshot_scope:
        reasons.append("bridge_packet_frozen_scope_mismatch")
    if not isinstance(packet.get("completion_contract"), dict) or not packet.get("completion_contract"):
        reasons.append("bridge_packet_missing_completion_contract")
    if not isinstance(packet.get("report_contract"), dict) or not packet.get("report_contract"):
        reasons.append("bridge_packet_missing_report_contract")
    reasons.extend(validate_dispatch_contract(packet, packet.get("dispatch_contract")))
    reasons.extend(_validate_packet_policy_fields(packet, snapshot))
    if str(target_phase) == "l4_implement" and not _packet_has_write_authority(packet):
        reasons.append("bridge_packet_implement_requires_write_authority")
    return reasons


def _validate_packet_policy_fields(packet: dict[str, Any], snapshot: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    completion = packet.get("completion_contract") if isinstance(packet.get("completion_contract"), dict) else {}
    report = packet.get("report_contract") if isinstance(packet.get("report_contract"), dict) else {}
    task_spec = packet.get("task_spec") if isinstance(packet.get("task_spec"), dict) else {}
    policy_ref = packet.get("policy_contract_ref") if isinstance(packet.get("policy_contract_ref"), dict) else {}
    if "report" not in set(completion.get("required_outputs", [])):
        reasons.append("bridge_packet_completion_contract_not_policy_owned")
    if not isinstance(completion.get("timeout_policy"), dict) or completion.get("timeout_policy", {}).get("timeout_action") != "ask_main_leader":
        reasons.append("bridge_packet_completion_contract_not_policy_owned")
    if not {"summary", "evidence"}.issubset(set(report.get("required_sections", []))):
        reasons.append("bridge_packet_report_contract_not_policy_owned")
    if "runtime event ids" not in set(report.get("required_evidence", [])):
        reasons.append("bridge_packet_report_contract_not_policy_owned")
    if "semantic_identity_resolution" not in set(report.get("required_sections", [])):
        reasons.append("bridge_packet_report_contract_not_policy_owned")
    if "semantic identity resolution" not in set(report.get("required_evidence", [])):
        reasons.append("bridge_packet_report_contract_not_policy_owned")
    if report.get("include_failure_reason") is not True or report.get("include_next_action_recommendation") is not True:
        reasons.append("bridge_packet_report_contract_not_policy_owned")
    semantic_contract = task_spec.get("semantic_resolution_contract")
    if not isinstance(semantic_contract, dict) or not semantic_contract.get("required_identity_fields"):
        reasons.append("bridge_packet_missing_semantic_resolution_contract")
    if policy_ref.get("source") == "control/policy/phase_contracts.json":
        taxonomy = report.get("classification_taxonomy")
        if not isinstance(taxonomy, dict):
            reasons.append("bridge_packet_missing_classification_taxonomy")
        else:
            for key in ("common", "coverage", "semantic_disposition"):
                if not isinstance(taxonomy.get(key), list) or not taxonomy.get(key):
                    reasons.append("bridge_packet_missing_classification_taxonomy")
                    break
    if str(packet.get("target_phase")) == "l4_execute":
        if "log_manifest" not in set(completion.get("required_artifacts", [])):
            reasons.append("bridge_packet_execute_log_manifest_contract_missing")
        if "log manifest path" not in set(report.get("required_evidence", [])):
            reasons.append("bridge_packet_execute_log_manifest_contract_missing")
        if "manifest required fields checklist" not in set(report.get("required_evidence", [])):
            reasons.append("bridge_packet_execute_log_manifest_contract_missing")
        if "batchbasis" not in set(report.get("required_evidence", [])):
            reasons.append("bridge_packet_execute_log_manifest_contract_missing")
        if "gpu_id" not in set(report.get("required_evidence", [])):
            reasons.append("bridge_packet_execute_log_manifest_contract_missing")
        if "natural-language model dataset method semantics" not in set(report.get("required_evidence", [])):
            reasons.append("bridge_packet_execute_log_manifest_contract_missing")
        if "artifact_manifests" not in set(report.get("required_sections", [])):
            reasons.append("bridge_packet_execute_log_manifest_contract_missing")
        if policy_ref.get("source") == "control/policy/phase_contracts.json":
            manifest_fields = completion.get("manifest_required_fields")
            if not isinstance(manifest_fields, list) or not {"run_id", "bridge_window_id", "task_id", "command", "cwd", "terminal_status"}.issubset({str(item) for item in manifest_fields}):
                reasons.append("bridge_packet_execute_manifest_schema_missing")
    if packet.get("approval_requirements") not in (None, []):
        reasons.append("bridge_packet_approval_requirements_not_runtime_owned")
    if packet.get("expires_at") is not None:
        reasons.append("bridge_packet_expiry_not_runtime_owned")
    if packet.get("phase_route") != _expected_phase_route(snapshot, str(packet.get("target_phase") or "")):
        reasons.append("bridge_packet_phase_route_not_policy_owned")
    if str(packet.get("target_phase")) == "l3_bridge" and not _packet_l3_write_scope_policy_owned(packet):
        reasons.append("bridge_packet_l3_write_scope_not_policy_owned")
    return reasons


def _expected_phase_route(snapshot: dict[str, Any], target_phase: str) -> list[str]:
    route = snapshot.get("route", {}).get("current_route")
    if isinstance(route, list) and route and str(route[-1]) == target_phase:
        return [str(item) for item in route]
    current = str(snapshot.get("current_phase") or "leader_freeze")
    return [current] if current == target_phase else [current, target_phase]


def _packet_target_matches_explicit_reroute(snapshot: dict[str, Any], target_phase: str) -> bool:
    route_state = snapshot.get("route")
    if not isinstance(route_state, dict) or route_state.get("is_stale") is True:
        return False
    if str(route_state.get("target_phase") or "") != target_phase:
        return False
    route = route_state.get("current_route")
    return isinstance(route, list) and bool(route) and str(route[-1]) == target_phase


def _packet_l3_write_scope_policy_owned(packet: dict[str, Any]) -> bool:
    expected_writable_scopes = {"."}
    team_spec = packet.get("team_spec") if isinstance(packet.get("team_spec"), dict) else {}
    ownership = team_spec.get("ownership_boundary") if isinstance(team_spec, dict) else {}
    if not isinstance(ownership, dict):
        return False
    writable_scopes = {str(item) for item in ownership.get("writable_scopes", []) if str(item)}
    return writable_scopes == expected_writable_scopes


def _packet_has_write_authority(packet: dict[str, Any]) -> bool:
    write_tools = {"Bash", "Edit", "Write", "MultiEdit"}
    packet_tools = {str(item) for item in packet.get("allowed_tools", []) if str(item)}
    team_spec = packet.get("team_spec") if isinstance(packet.get("team_spec"), dict) else {}
    ownership = team_spec.get("ownership_boundary") if isinstance(team_spec, dict) else {}
    writable_scopes = ownership.get("writable_scopes") if isinstance(ownership, dict) else []
    teammate_specs = team_spec.get("teammate_specs") if isinstance(team_spec, dict) else []
    teammate_tools: set[str] = set()
    if isinstance(teammate_specs, list):
        for teammate in teammate_specs:
            if not isinstance(teammate, dict):
                continue
            teammate_tools.update(str(item) for item in teammate.get("allowed_tools", []) if str(item))
    return bool(write_tools & (packet_tools | teammate_tools)) and bool(writable_scopes)


def _validate_task_created_payload(event: WorkflowEvent, payload: dict[str, Any], binding: dict[str, Any] | None) -> list[str]:
    reasons: list[str] = []
    description = str(payload.get("task_description") or "").strip()
    task_spec = payload.get("task_spec")
    team_spec = payload.get("team_spec")
    mapping = payload.get("task_team_mapping")
    if not description or not isinstance(task_spec, dict) or not isinstance(team_spec, dict) or not isinstance(mapping, dict):
        reasons.append("taskcreated_payload_incomplete")
        return reasons
    if not task_spec.get("task_subject") or not task_spec.get("task_kind"):
        reasons.append("taskcreated_payload_incomplete")
    if not team_spec.get("team_name") and not event.team_id:
        reasons.append("taskcreated_team_binding_invalid")
    assignments = mapping.get("teammate_assignments")
    if not isinstance(assignments, list) or len(assignments) == 0:
        reasons.append("taskcreated_mapping_invalid")
    if binding:
        if event.team_id and binding.get("team_id_or_null") not in {None, event.team_id}:
            reasons.append("taskcreated_team_binding_invalid")
        if event.task_id and binding.get("task_id_or_null") not in {None, event.task_id}:
            reasons.append("taskcreated_mapping_invalid")
    return reasons


def _completion_contract_satisfied(contract: dict[str, Any], payload: dict[str, Any], checks: dict[str, Any]) -> bool:
    required_outputs = contract.get("required_outputs", [])
    required_artifacts = contract.get("required_artifacts", [])
    if required_outputs and not checks.get("required_outputs_present", False):
        return False
    if required_artifacts and not checks.get("required_artifacts_present", False):
        return False
    validation_requirements = contract.get("validation_requirements", [])
    if validation_requirements and not checks.get("validation_passed", False):
        return False
    if required_outputs and not payload.get("reports"):
        return False
    if required_artifacts and not payload.get("artifact_refs"):
        return False
    missing_outputs = set(checks.get("missing_outputs", []))
    missing_artifacts = set(checks.get("missing_artifacts", []))
    return not missing_outputs and not missing_artifacts


def _recommendation_for_denial(event: WorkflowEvent, snapshot: dict[str, Any], check_result: CheckResult) -> str:
    reasons = set(check_result.reasons)
    if event.event_kind in {"bridge_call_intended", "pretooluse_allowed_by_main_leader", "call_bridge_sdk_started"}:
        return _recommended_reroute_action(snapshot)
    if "bridge_packet_route_not_allowed" in reasons or "illegal_phase_transition" in reasons:
        return _recommended_reroute_action(snapshot)
    if "approval_pending_blocks_bridge_call" in reasons:
        return "resolve_approval_or_explain_why_approval_remains_pending_before_reroute"
    if "hard_stop_blocks_bridge_call" in reasons:
        return "clear_hard_stop_or_explain_why_hard_stop_remains_before_reroute"
    if "bridge_packet_semantic_refresh_required" in reasons:
        return "reroute_to_leader_freeze_and_refreeze_semantics"
    return "read_runtime_snapshot_then_reroute_to_recommended_next_phase_or_explain_no_reroute"


def _recommended_reroute_action(snapshot: dict[str, Any]) -> str:
    allowed_routes = [str(item) for item in snapshot.get("allowed_routes", []) if str(item)]
    if allowed_routes:
        preferred = allowed_routes[0]
        return f"reroute_phase:{preferred}; if not correct, choose another legal allowed_route and record the reason"
    current = str(snapshot.get("current_phase") or "unknown")
    return f"no_legal_allowed_route_from:{current}; explain why reroute is impossible and ask for leader_freeze/user direction"


def _notify_item(level: str, category: str, message: str, event: WorkflowEvent, recommended_action: str | None) -> dict[str, Any]:
    related_ids = {"run_id": event.run_id}
    for key in ("main_session_id", "sub_session_id", "bridge_window_id", "team_id", "task_id", "tool_use_id"):
        value = getattr(event, key)
        if value:
            related_ids[key] = value
    return {
        "level": level,
        "category": category,
        "message": message,
        "related_ids": related_ids,
        "recommended_action_or_null": recommended_action,
    }


def _dedupe_notify_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    priority = {"blocking": 0, "error": 1, "warn": 2, "info": 3}
    for item in sorted(items, key=lambda item: priority.get(str(item.get("level")), 9)):
        key = (str(item.get("level")), str(item.get("category")), str(item.get("message")))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _changed_top_level_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    keys = sorted(set(before) | set(after))
    return [key for key in keys if before.get(key) != after.get(key)]


def _empty_bindings() -> dict[str, Any]:
    return {"bridge_windows": {}, "teams": {}, "tasks": {}, "tool_uses": {}}


def _base_run_for_replay(
    current_run: dict[str, Any],
    run_id: str,
    *,
    first_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event_payload = first_event or {}
    embedded_payload = event_payload.get("payload") if isinstance(event_payload.get("payload"), dict) else {}
    packet = embedded_payload.get("packet") if isinstance(embedded_payload.get("packet"), dict) else {}
    route = packet.get("phase_route") if isinstance(packet.get("phase_route"), list) else []
    timestamp = str(event_payload.get("timestamp") or current_run.get("created_at") or _now_iso())
    pure_replay = first_event is not None
    main_session_id = str(event_payload.get("main_session_id") or (None if pure_replay else current_run.get("main_session_id")) or run_id)
    semantic_seed = {"frozen": packet.get("frozen_semantics"), "frozen_at": timestamp if packet.get("frozen_semantics") is not None else None, "requires_refresh": False}
    scope_seed = {"frozen": packet.get("frozen_scope"), "frozen_at": timestamp if packet.get("frozen_scope") is not None else None, "requires_refresh": False}
    route_seed = {
        "current_route": route,
        "target_phase": packet.get("target_phase"),
        "is_stale": False,
        "decided_by_event_id": None,
    }
    base = {
        "schema_version": SCHEMA_VERSION if pure_replay else current_run.get("schema_version") or SCHEMA_VERSION,
        "run_id": run_id,
        "main_session_id": main_session_id,
        "workflow_name": "bridge_window_workflow" if pure_replay else current_run.get("workflow_name") or "bridge_window_workflow",
        "workflow_version": SCHEMA_VERSION if pure_replay else current_run.get("workflow_version") or SCHEMA_VERSION,
        "run_status": "in_progress" if pure_replay else current_run.get("run_status") if current_run.get("run_status") in {"completed", "aborted", "failed"} else "in_progress",
        "current_phase": str(route[0]) if route else ("leader_freeze" if pure_replay else current_run.get("current_phase") or "leader_freeze"),
        "semantic": deepcopy(semantic_seed if pure_replay else current_run.get("semantic") or semantic_seed),
        "scope": deepcopy(scope_seed if pure_replay else current_run.get("scope") or scope_seed),
        "route": deepcopy(route_seed if pure_replay else current_run.get("route") or route_seed),
        "approval_state": deepcopy({"pending": False, "active_approval_ids": [], "records": []} if pure_replay else current_run.get("approval_state") or {"pending": False, "active_approval_ids": [], "records": []}),
        "hard_stop": deepcopy({"active": False, "reason_code": None, "details": None, "task_id": None, "raised_at": None} if pure_replay else current_run.get("hard_stop") or {"active": False, "reason_code": None, "details": None, "task_id": None, "raised_at": None}),
        "created_at": timestamp if pure_replay else current_run.get("created_at") or timestamp,
        "updated_at": timestamp,
        "closed_at": None if pure_replay else current_run.get("closed_at"),
    }
    _ensure_workflow_indexes(base)
    return base


def _persist_reconcile_replay(
    paths: ControlPaths,
    run_id: str,
    run_ledger: dict[str, Any],
    snapshot: dict[str, Any],
    reconcile_result: dict[str, Any],
) -> None:
    run_root = paths.run_root(run_id)
    atomic_write_json(paths.run_ledger_path(run_id), run_ledger)
    atomic_write_json(run_root / "runtime_snapshot.json", snapshot)
    atomic_write_json(run_root / "reconcile_result.json", reconcile_result)
    transitions = run_ledger.get("workflow_transitions", [])
    transitions_path = paths.transitions_path(run_id)
    transitions_path.parent.mkdir(parents=True, exist_ok=True)
    transitions_path.write_text(
        "".join(f"{json_line}\n" for json_line in [_json_dumps(record) for record in transitions]),
        encoding="utf-8",
    )


def _json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(sanitize_json_value(payload), ensure_ascii=False)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _normalize_agent_type(value: Any) -> str:
    raw = str(value or "runtime").strip()
    return AGENT_TYPE_ALIASES.get(raw, raw)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
