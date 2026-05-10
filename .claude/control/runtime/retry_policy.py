from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loader import load_json_file
from state_graph import stable_hash


DEFAULT_RETRY_POLICIES: dict[str, dict[str, Any]] = {
    "bridge_sdk_call": {
        "initial_interval_ms": 2000,
        "backoff_coefficient": 2.0,
        "maximum_interval_ms": 30000,
        "maximum_attempts": 3,
        "non_retryable_error_types": [
            "PacketBindingMismatch",
            "TargetRepoBoundaryViolation",
            "PendingApproval",
            "FrozenSemanticsMismatch",
        ],
    },
    "teammate_report_missing": {
        "initial_interval_ms": 5000,
        "backoff_coefficient": 1.5,
        "maximum_interval_ms": 60000,
        "maximum_attempts": 4,
    },
    "completion_rejected": {
        "initial_interval_ms": 0,
        "backoff_coefficient": 1.0,
        "maximum_interval_ms": 0,
        "maximum_attempts": 2,
        "requires_same_packet_boundary": True,
    },
    "l4_execute_process_poll": {
        "heartbeat_timeout_ms": 300000,
        "maximum_attempts": 0,
        "retry_until_terminal_process_state": True,
    },
}


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retry_scope: str
    attempt: int
    max_attempts: int
    delay_ms: int
    retryable: bool
    exhausted: bool
    reason: dict[str, Any]
    next_action: str
    policy: dict[str, Any]

    def as_event_payload(
        self,
        *,
        repo_key: str,
        run_id: str,
        bridge_window_id: str | None,
        packet_hash: str | None,
    ) -> dict[str, Any]:
        return {
            "event_type": "retry_attempt_scheduled",
            "timestamp": _now_iso(),
            "repo_key": repo_key,
            "run_id": run_id,
            "bridge_window_id": bridge_window_id,
            "retry_scope": self.retry_scope,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "delay_ms": self.delay_ms,
            "packet_hash": packet_hash,
            "reason": self.reason,
            "retryable": self.retryable,
            "exhausted": self.exhausted,
            "next_action": self.next_action,
        }


def load_retry_policies(control_root: str | Path) -> dict[str, dict[str, Any]]:
    root = Path(control_root).expanduser().resolve()
    contracts = load_json_file(root / "policy" / "phase_contracts.json", default={}) or {}
    configured = contracts.get("retry_policies") if isinstance(contracts.get("retry_policies"), dict) else {}
    policies = {key: dict(value) for key, value in DEFAULT_RETRY_POLICIES.items()}
    for key, value in configured.items():
        if isinstance(value, dict):
            merged = dict(policies.get(str(key), {}))
            merged.update(value)
            policies[str(key)] = merged
    return policies


def decide_retry(
    policies: dict[str, dict[str, Any]],
    retry_scope: str,
    *,
    attempt: int,
    error_type: str | None,
    reason: dict[str, Any] | None = None,
) -> RetryDecision:
    policy = dict(policies.get(retry_scope) or DEFAULT_RETRY_POLICIES.get(retry_scope) or {})
    max_attempts = int(policy.get("maximum_attempts", 1) or 0)
    non_retryable = {str(item) for item in policy.get("non_retryable_error_types", []) if str(item)}
    current_attempt = max(1, int(attempt))
    error = str(error_type or "UnknownError")
    if error in non_retryable:
        retryable = False
        exhausted = False
    elif max_attempts == 0:
        retryable = True
        exhausted = False
    else:
        retryable = current_attempt <= max_attempts
        exhausted = current_attempt > max_attempts
    delay_ms = _delay_ms(policy, current_attempt)
    if error in non_retryable:
        next_action = "surface_non_retryable_failure"
    elif exhausted:
        next_action = "enter_anomaly_or_surface_failure"
    elif retry_scope == "completion_rejected":
        next_action = "retry_same_packet_repair_output"
    elif retry_scope == "l4_execute_process_poll":
        next_action = "poll_until_terminal_process_state"
    else:
        next_action = "retry_same_packet"
    return RetryDecision(
        retry_scope=retry_scope,
        attempt=current_attempt,
        max_attempts=max_attempts,
        delay_ms=delay_ms,
        retryable=retryable,
        exhausted=exhausted,
        reason={**(reason or {}), "error_type": error, "retryable": retryable},
        next_action=next_action,
        policy=policy,
    )


def packet_hash(packet: dict[str, Any] | None) -> str | None:
    if not isinstance(packet, dict):
        return None
    return stable_hash(packet)


def _delay_ms(policy: dict[str, Any], attempt: int) -> int:
    initial = int(policy.get("initial_interval_ms", 0) or 0)
    maximum = int(policy.get("maximum_interval_ms", initial) or initial)
    coefficient = float(policy.get("backoff_coefficient", 1.0) or 1.0)
    if initial <= 0:
        return 0
    delay = int(initial * (coefficient ** max(0, attempt - 1)))
    return min(delay, maximum if maximum > 0 else delay)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
