from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

PhaseName = Literal[
    "leader_freeze",
    "l2_advisory",
    "l3_bridge",
    "l4_implement",
    "l4_execute",
    "l4_anomaly",
]

RunStatus = Literal[
    "initialized",
    "in_progress",
    "awaiting_approval",
    "paused",
    "blocked_for_user_clarification",
    "paused_for_user_answer",
    "user_answer_received",
    "resume_same_l3_task",
    "continuation_of_previous_l3",
    "blocked",
    "failed",
    "completed",
    "aborted",
]

TaskStatus = Literal[
    "created",
    "ready",
    "in_progress",
    "waiting_on_dependency",
    "waiting_on_approval",
    "blocked",
    "retryable_failure",
    "completed",
    "failed",
    "aborted",
    "noop",
]

ActionName = Literal[
    "create_task",
    "start_task",
    "move_task_to_waiting",
    "block_task",
    "retry_task",
    "complete_task",
    "fail_task",
    "abort_task",
    "noop_task",
    "advance_phase",
    "reroute_phase",
    "pause_run",
    "resume_run",
    "request_approval",
    "resolve_approval",
    "mark_hard_stop",
    "clear_hard_stop",
    "complete_run",
    "abort_run",
]

Decision = Literal["allowed", "denied", "blocked", "noop"]
ReconcileMode = Literal["authoritative", "recovery"]


@dataclass(slots=True)
class ActionRequest:
    run_id: str
    action: ActionName
    task_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    requester: str = "system"
    timestamp: str = ""
    trigger_source: Literal["system", "reconcile", "manual", "policy", "hook"] = "system"
    hook_name: str | None = None
    event_name: str | None = None
    request_id: str | None = None


@dataclass(slots=True)
class GuardResult:
    guard_type: str
    guard_name: str
    result: Literal["pass", "fail", "warn", "skip"]
    details: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "guard_type": self.guard_type,
            "guard_name": self.guard_name,
            "result": self.result,
            "details": self.details,
        }


@dataclass(slots=True)
class ValidationResult:
    decision: Decision
    reason_code: str
    reason: str
    guard_results: list[GuardResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "guard_results": [g.as_dict() for g in self.guard_results],
        }


@dataclass(slots=True)
class LoadedState:
    control_root: str
    runtime_runs_root: str
    run_ledger: dict[str, Any]
    task_ledgers: dict[str, dict[str, Any]]
    transition_records: list[dict[str, Any]]
    phase_graph: dict[str, Any]
    approval_matrix: dict[str, Any]
    reconcile_rules: dict[str, Any]


@dataclass(slots=True)
class ReconcileOutput:
    reconcile_result: dict[str, Any]
    run_ledger: dict[str, Any]


@dataclass(slots=True)
class DispatchResult:
    ok: bool
    transition_id: str
    run_id: str
    task_id: str | None
    decision: Decision
    run_status: str
    current_phase: str
    allowed_next_actions: list[str]
    integrity_alerts: list[dict[str, Any]] = field(default_factory=list)
    transition_record: dict[str, Any] = field(default_factory=dict)
    task_ledgers: dict[str, dict[str, Any]] = field(default_factory=dict)
    reconcile_result: dict[str, Any] = field(default_factory=dict)
    run_ledger: dict[str, Any] = field(default_factory=dict)
