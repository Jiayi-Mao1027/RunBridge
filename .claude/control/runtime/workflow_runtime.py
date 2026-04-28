from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import uuid
from pathlib import Path
from typing import Any

from loader import ControlPaths, load_json_file, load_jsonl
from persist import append_jsonl, atomic_write_json


SCHEMA_VERSION = "0.4.0"

AGENT_TYPES = {"main-leader", "bridge-leader", "teammate", "hook", "runtime"}
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
    "artifacts_ready",
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
}

LIFECYCLE_TRANSITIONS: dict[str | None, dict[str, str]] = {
    None: {
        "bridge_call_intended": "bridge_call_intended",
        "pretooluse_denied_by_main_leader": "bridge_call_denied",
    },
    "bridge_call_intended": {
        "pretooluse_allowed_by_main_leader": "bridge_call_prechecked",
        "pretooluse_denied_by_main_leader": "bridge_call_denied",
    },
    "bridge_call_prechecked": {
        "call_bridge_sdk_started": "bridge_call_started",
        "call_bridge_sdk_error": "bridge_call_failed",
    },
    "bridge_call_started": {
        "bridge_window_opened": "bridge_window_opened",
        "call_bridge_sdk_error": "bridge_call_failed",
        "orphan_timeout_without_bridge_return": "bridge_window_orphaned",
    },
    "bridge_window_opened": {
        "bridge_packet_accepted": "bridge_packet_accepted",
        "bridge_packet_rejected": "bridge_packet_rejected",
        "orphan_timeout_without_bridge_return": "bridge_window_orphaned",
    },
    "bridge_packet_rejected": {"bridge_result_returned": "bridge_window_returned"},
    "bridge_packet_accepted": {
        "team_create_started": "team_create_started",
        "orphan_timeout_without_bridge_return": "bridge_window_orphaned",
    },
    "team_create_started": {
        "team_create_succeeded": "team_create_completed",
        "team_create_failed": "team_create_failed",
    },
    "team_create_failed": {"bridge_result_returned": "bridge_window_failed"},
    "team_create_completed": {"task_create_started": "task_create_started"},
    "task_create_started": {
        "task_create_succeeded": "task_create_completed",
        "task_create_failed": "task_create_failed",
    },
    "task_create_failed": {"bridge_result_returned": "bridge_window_failed"},
    "task_create_completed": {
        "taskcreated_hook_accepted": "task_created_recorded",
        "taskcreated_hook_denied": "task_create_failed",
    },
    "task_created_recorded": {"message_dispatch_started": "message_dispatch_started"},
    "message_dispatch_started": {
        "message_dispatch_succeeded": "message_dispatch_completed",
        "message_dispatch_failed": "message_dispatch_failed",
    },
    "message_dispatch_failed": {
        "message_dispatch_retry_started": "message_dispatch_started",
        "bridge_leader_fails_task": "task_failed",
        "bridge_result_returned": "bridge_window_failed",
    },
    "message_dispatch_completed": {"team_idle_waiting": "team_waiting", "artifacts_ready": "task_completion_started"},
    "team_waiting": {
        "team_idle_waiting": "team_waiting",
        "artifacts_ready": "task_completion_started",
        "wait_timeout_or_process_lost": "team_wait_timeout",
        "orphan_timeout_without_heartbeat": "bridge_window_orphaned",
    },
    "team_wait_timeout": {
        "partial_evidence_collected": "bridge_window_partial_returned",
        "task_failed_by_bridge_leader": "task_failed",
        "bridge_result_returned": "bridge_window_partial_returned",
    },
    "task_completion_started": {
        "completion_contract_satisfied": "task_completion_completed",
        "completion_contract_rejected": "task_completion_rejected",
    },
    "task_completion_rejected": {
        "continue_waiting": "team_waiting",
        "retry_artifact_collection": "task_completion_started",
        "bridge_leader_fails_task": "task_failed",
        "bridge_result_returned": "bridge_window_failed",
    },
    "task_completion_completed": {"team_delete_started": "team_delete_started"},
    "task_failed": {"team_delete_started": "team_delete_started", "bridge_result_returned": "bridge_window_failed"},
    "bridge_window_partial_returned": {"team_delete_started": "team_delete_started"},
    "team_delete_started": {
        "team_delete_succeeded": "team_delete_completed",
        "team_delete_failed": "team_delete_failed",
    },
    "team_delete_completed": {"bridge_result_returned": "bridge_window_returned"},
    "team_delete_failed": {"bridge_result_returned_with_cleanup_required": "bridge_window_partial_returned"},
}

EVENT_TO_UPDATE_KIND = {
    "user_prompt_submitted": "record_user_prompt_submitted",
    "phase_advanced": "advance_phase",
    "route_rerouted": "reroute_phase",
    "bridge_call_intended": "record_bridge_call_intent",
    "pretooluse_allowed_by_main_leader": "record_bridge_call_prechecked",
    "pretooluse_denied_by_main_leader": "record_bridge_call_denied",
    "call_bridge_sdk_error": "record_bridge_call_failed",
    "bridge_window_opened": "register_bridge_window_open",
    "bridge_packet_rejected": "persist_bridge_packet_rejected",
    "team_create_succeeded": "persist_team_created",
    "team_create_failed": "persist_team_create_failed",
    "taskcreated_hook_accepted": "persist_task_created",
    "taskcreated_hook_denied": "persist_task_create_failed",
    "task_create_failed": "persist_task_create_failed",
    "message_dispatch_succeeded": "persist_message_dispatched",
    "message_dispatch_failed": "persist_message_dispatch_failed",
    "team_idle_waiting": "persist_team_waiting",
    "wait_timeout_or_process_lost": "persist_team_wait_timeout",
    "completion_contract_satisfied": "persist_task_completed",
    "completion_contract_rejected": "persist_task_completion_rejected",
    "bridge_leader_fails_task": "persist_task_failed",
    "task_failed_by_bridge_leader": "persist_task_failed",
    "team_delete_succeeded": "persist_team_deleted",
    "team_delete_failed": "persist_team_delete_failed",
    "bridge_result_returned": "persist_bridge_result_returned",
    "bridge_result_returned_with_cleanup_required": "persist_bridge_result_returned",
    "orphan_timeout_without_bridge_return": "persist_bridge_window_orphaned",
    "orphan_timeout_without_heartbeat": "persist_bridge_window_orphaned",
}

CONTROL_EVENTS_WITHOUT_BRIDGE_LIFECYCLE = {"user_prompt_submitted", "phase_advanced", "route_rerouted"}

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
            agent_type=str(payload.get("agent_type") or "runtime"),
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
    persist: bool = False,
) -> WorkflowDispatchResult:
    paths = ControlPaths.from_root(control_root, runtime_runs_root)
    run_id = str(event_payload.get("run_id") or "").strip()
    existing_run = load_json_file(paths.run_ledger_path(run_id), default={}) or {}
    event = WorkflowEvent.from_payload(event_payload, existing_run)
    if not event.run_id:
        raise ValueError("workflow event requires run_id")

    run_ledger = _ensure_run_ledger(existing_run, event)
    snapshot_before = build_runtime_snapshot(paths, run_ledger)
    lifecycle_transitions = load_lifecycle_transitions(paths)
    allowed_policy_events = load_allowed_policy_events(paths)
    check_result = check_event(event, snapshot_before, lifecycle_transitions, allowed_policy_events)
    update_kind = EVENT_TO_UPDATE_KIND.get(event.event_kind, "generic_runtime_update")

    if check_result.decision != "allow" and update_kind not in FAILURE_UPDATE_KINDS:
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


def check_event(
    event: WorkflowEvent,
    snapshot: dict[str, Any],
    lifecycle_transitions: dict[str | None, dict[str, str]] | None = None,
    allowed_policy_events: set[str] | None = None,
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
        if not isinstance(contract, dict) or not contract:
            reasons.append("completion_contract_missing")
        elif not _completion_contract_satisfied(contract, normalized_payload, checks):
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

    to_status = check_result.derived_facts.get("to_status")
    if to_status is None:
        to_status = _resolve_transition(lifecycle_transitions or LIFECYCLE_TRANSITIONS, _status_for_run(run, event), event.event_kind)
    if to_status is None:
        to_status = event.event_kind

    transition = _build_transition(event, update_kind, check_result, to_status)
    _apply_transition_to_run(run, event, to_status, transition)
    run.setdefault("recent_events", []).append(event_record)
    run["recent_events"] = run["recent_events"][-50:]

    if event.event_kind in {"bridge_result_returned", "bridge_result_returned_with_cleanup_required"}:
        run["last_bridge_result"] = _build_bridge_result(event, to_status)

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
                "stop_current_action_and_replan",
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
        "call_bridge_sdk_error": ("error", "bridge_call_failed", "read_runtime_snapshot_and_decide_retry_or_report"),
        "bridge_packet_rejected": ("error", "bridge_packet_rejected", "rebuild_packet_from_runtime_truth_or_report_blocked"),
        "team_create_failed": ("error", "team_create_failed", "retry_bridge_window_or_report_failure"),
        "task_create_failed": ("error", "task_create_failed", "delete_team_if_created_then_rebuild_task_packet"),
        "message_dispatch_failed": ("warn", "message_dispatch_failed", "retry_send_or_fail_task_inside_same_bridge_window"),
        "team_idle_waiting": ("info", "team_waiting", "continue_waiting_or_poll_according_to_timeout_policy"),
        "wait_timeout_or_process_lost": ("error", "team_wait_timeout", "collect_partial_evidence_then_decide_retry_or_fail"),
        "completion_contract_rejected": ("warn", "task_completion_rejected", "continue_waiting_retry_collection_or_fail_task"),
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
    integrity = _derive_integrity(run)
    lifecycle = _derive_lifecycle(run)
    phase_exit_readiness = _derive_phase_exit_readiness(run)

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run["run_id"],
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
        "bindings": run.get("bindings", _empty_bindings()),
        "allowed_actions": _derive_allowed_actions(integrity, lifecycle),
        "allowed_routes": allowed_routes,
        "integrity": integrity,
        "last_bridge_result": run.get("last_bridge_result"),
        "phase_exit_readiness": phase_exit_readiness,
    }
    return snapshot


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
    event_path = run_root / "event_log.jsonl"
    check_path = run_root / "check_ledger.jsonl"
    update_path = run_root / "update_ledger.jsonl"
    notify_path = run_root / "main_leader_inbox.jsonl"
    snapshot_path = run_root / "runtime_snapshot.json"

    append_jsonl(event_path, event.as_record())
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
    for transition in run_ledger.get("workflow_transitions", [])[-1:]:
        append_jsonl(paths.transitions_path(event.run_id), transition)

    atomic_write_json(paths.run_ledger_path(event.run_id), run_ledger)
    atomic_write_json(snapshot_path, snapshot)
    return {
        "event_log": str(event_path),
        "check_ledger": str(check_path),
        "update_ledger": str(update_path),
        "main_leader_inbox": str(notify_path),
        "runtime_snapshot": str(snapshot_path),
        "run_ledger": str(paths.run_ledger_path(event.run_id)),
        "transitions": str(paths.transitions_path(event.run_id)),
    }


def load_recent_workflow_events(paths: ControlPaths, run_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    records = load_jsonl(paths.run_root(run_id) / "event_log.jsonl")
    return records[-limit:]


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
    bridge_window_id = event.bridge_window_id
    if bridge_window_id:
        binding = run["bindings"]["bridge_windows"].setdefault(
            bridge_window_id,
            _new_bridge_binding(event),
        )
        binding["updated_at"] = event.timestamp
        binding["lifecycle_status"] = to_status
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
        run["bindings"]["teams"].setdefault(
            event.team_id,
            {
                "run_id": event.run_id,
                "sub_session_id": event.sub_session_id,
                "bridge_window_id": event.bridge_window_id,
                "team_id": event.team_id,
                "team_name": event.payload.get("team_name") or event.team_id,
                "teammate_ids": event.payload.get("teammate_ids", []),
                "owner_agent_id": event.agent_id,
                "owner_agent_type": "bridge-leader",
            },
        )

    if event.task_id:
        run["bindings"]["tasks"].setdefault(
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
    run["workflow_transitions"] = run["workflow_transitions"][-200:]


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
    return {
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


def _derive_integrity(run: dict[str, Any]) -> dict[str, Any]:
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
    return {
        "has_hard_stop": bool(hard_stop.get("active", False)),
        "awaiting_approval": bool(approval.get("pending", False)),
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
    if binding.get("main_session_id") not in {None, event.main_session_id, snapshot.get("main_session_id")}:
        reasons.append("bridge_packet_binding_mismatch")
    if not event.bridge_window_id or binding.get("bridge_window_id") != event.bridge_window_id:
        reasons.append("bridge_packet_binding_mismatch")
    if not event.sub_session_id or binding.get("sub_session_id") != event.sub_session_id:
        reasons.append("bridge_packet_binding_mismatch")
    allowed_routes = set(snapshot.get("allowed_routes", []))
    target_phase = packet.get("target_phase")
    if target_phase and allowed_routes and target_phase not in allowed_routes and target_phase != snapshot.get("current_phase"):
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
    return reasons


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


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
