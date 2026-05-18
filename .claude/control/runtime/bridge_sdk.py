from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import time
from typing import Any
import uuid

from bridge_leader import TeamExecutor, execute_bridge_window
from dispatch_contract import build_dispatch_contract
from workflow_runtime import dispatch_workflow_event


BRIDGE_LEADER_RETRYABLE_ERROR_TYPES = {
    "BridgeLeaderNoReport",
    "ClaudeCliFailed",
    "ClaudeCliInvalidReports",
    "ClaudeCliInvalidPayload",
    "ClaudeCliEmptyResult",
    "ClaudeCliNonJsonOutput",
    "ClaudeCliNeedsToolContinuation",
    "RuntimeOwnedTeammateFallbackFailed",
    "RuntimeOwnedTeammateNoJsonReport",
}

PROVIDER_TRANSPORT_ERROR_TYPES = {
    "ProviderTransportApiError",
    "ProviderTransportConnectionRefused",
    "ProviderTransportRateLimited",
    "ProviderTransportReset",
    "ProviderGateTimeout",
}


def call_bridge_sdk(
    control_root: str | Path,
    packet: dict[str, Any],
    *,
    runtime_runs_root: str | Path | None = None,
    team_executor: TeamExecutor | None = None,
    persist: bool = True,
    record_main_lifecycle: bool = True,
) -> dict[str, Any]:
    """Run one bridge invocation window.

    When called from a real tool path, PreToolUse may already have recorded
    bridge_call_intended/prechecked/started. In that case set
    record_main_lifecycle=False to avoid duplicate main-leader events.
    """
    current_packet = deepcopy(packet)
    attempts: list[dict[str, Any]] = []
    max_attempts = _bridge_leader_retry_max_attempts(current_packet)
    original_bridge_window_id = str(current_packet.get("binding", {}).get("bridge_window_id") or "")
    attempt_index = 1
    while True:
        binding = current_packet.get("binding", {})
        if record_main_lifecycle or attempt_index > 1:
            _record_main_bridge_start(control_root, current_packet, runtime_runs_root=runtime_runs_root, persist=persist)
        result = _call_bridge_once(
            control_root,
            current_packet,
            runtime_runs_root=runtime_runs_root,
            team_executor=team_executor,
            persist=persist,
            record_main_lifecycle=record_main_lifecycle or attempt_index > 1,
        )
        attempts.append(_bridge_leader_retry_attempt(attempt_index, current_packet, result))
        if not _bridge_leader_failure_retryable(result, current_packet) or attempt_index >= max_attempts:
            return _with_bridge_leader_retry_evidence(result, attempts, max_attempts=max_attempts)

        next_packet = _clone_packet_for_bridge_leader_retry(current_packet, attempt_index + 1)
        delay_ms = _bridge_leader_retry_delay_ms(current_packet, attempt_index)
        _record_bridge_leader_retry_scheduled(
            control_root,
            current_packet,
            next_packet,
            result,
            attempt=attempt_index + 1,
            max_attempts=max_attempts,
            delay_ms=delay_ms,
            original_bridge_window_id=original_bridge_window_id,
            runtime_runs_root=runtime_runs_root,
            persist=persist,
        )
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        current_packet = next_packet
        attempt_index += 1


def _call_bridge_once(
    control_root: str | Path,
    packet: dict[str, Any],
    *,
    runtime_runs_root: str | Path | None,
    team_executor: TeamExecutor | None,
    persist: bool,
    record_main_lifecycle: bool,
) -> dict[str, Any]:
    binding = packet.get("binding", {})
    try:
        return execute_bridge_window(
            control_root,
            packet,
            runtime_runs_root=runtime_runs_root,
            team_executor=team_executor,
            persist=persist,
        )
    except KeyboardInterrupt as exc:
        _record_main_bridge_interrupted(
            control_root,
            packet,
            runtime_runs_root=runtime_runs_root,
            persist=persist,
            error={"message": "bridge invocation interrupted by user", "type": exc.__class__.__name__},
        )
        return {
            "run_id": binding.get("run_id"),
            "main_session_id": binding.get("main_session_id"),
            "sub_session_id": binding.get("sub_session_id"),
            "bridge_window_id": binding.get("bridge_window_id"),
            "team_id_or_null": None,
            "task_id_or_null": None,
            "status": "failed",
            "failure_stage_or_null": "manual_interrupt",
            "reports": [
                {
                    "summary": "Bridge invocation was interrupted by the user.",
                    "failure_reason": "manual_interrupt",
                    "next_action_recommendation": "Read the runtime snapshot; the interrupted bridge window is terminal and a new legal bridge may be dispatched if no other blocker remains.",
                }
            ],
            "artifact_refs": [],
            "evidence": {"classification": "manual_bridge_interrupt"},
            "error_or_null": {"message": "bridge invocation interrupted by user", "type": exc.__class__.__name__},
            "cleanup_required": True,
        }
    except Exception as exc:
        if record_main_lifecycle:
            _record_main_bridge_error(
                control_root,
                packet,
                runtime_runs_root=runtime_runs_root,
                persist=persist,
                error={"message": str(exc), "type": exc.__class__.__name__},
            )
        return {
            "run_id": binding.get("run_id"),
            "main_session_id": binding.get("main_session_id"),
            "sub_session_id": binding.get("sub_session_id"),
            "bridge_window_id": binding.get("bridge_window_id"),
            "team_id_or_null": None,
            "task_id_or_null": None,
            "status": "failed",
            "failure_stage_or_null": "bridge_return",
            "reports": [],
            "artifact_refs": [],
            "evidence": None,
            "error_or_null": {"message": str(exc), "type": exc.__class__.__name__},
            "cleanup_required": False,
        }


def _bridge_leader_retry_max_attempts(packet: dict[str, Any]) -> int:
    raw = packet.get("retry_policies", {}).get("bridge_sdk_call") if isinstance(packet.get("retry_policies"), dict) else {}
    configured = raw if isinstance(raw, dict) else {}
    try:
        attempts = int(configured.get("maximum_attempts") or 1)
    except Exception:
        attempts = 1
    try:
        env_attempts = int(str(__import__("os").environ.get("BRIDGE_LEADER_RETRY_MAX_ATTEMPTS") or "").strip() or "0")
    except Exception:
        env_attempts = 0
    if env_attempts > 0:
        attempts = env_attempts
    return max(1, min(5, attempts))


def _bridge_leader_retry_delay_ms(packet: dict[str, Any], completed_attempt: int) -> int:
    raw = packet.get("retry_policies", {}).get("bridge_sdk_call") if isinstance(packet.get("retry_policies"), dict) else {}
    configured = raw if isinstance(raw, dict) else {}
    try:
        initial = int(configured.get("initial_interval_ms") or 0)
        maximum = int(configured.get("maximum_interval_ms") or initial)
        coefficient = float(configured.get("backoff_coefficient") or 1.0)
    except Exception:
        return 0
    if initial <= 0:
        return 0
    delay = int(initial * (coefficient ** max(0, completed_attempt - 1)))
    if maximum > 0:
        delay = min(delay, maximum)
    return max(0, delay)


def _bridge_leader_failure_retryable(result: dict[str, Any], packet: dict[str, Any]) -> bool:
    if not isinstance(result, dict) or result.get("status") == "succeeded":
        return False
    if result.get("cleanup_required") is True:
        return False
    error = result.get("error_or_null") if isinstance(result.get("error_or_null"), dict) else {}
    error_type = str(error.get("type") or "")
    if not error_type:
        return False
    if error_type in PROVIDER_TRANSPORT_ERROR_TYPES:
        return False
    policy = packet.get("retry_policies", {}).get("bridge_sdk_call") if isinstance(packet.get("retry_policies"), dict) else {}
    non_retryable = {
        str(item)
        for item in (policy.get("non_retryable_error_types") if isinstance(policy, dict) else []) or []
        if str(item)
    }
    if error_type in non_retryable:
        return False
    if error_type in BRIDGE_LEADER_RETRYABLE_ERROR_TYPES:
        return True
    evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    classification = str(evidence.get("failure_classification") or "")
    if classification == "provider_transport_failure" or evidence.get("cooldown_required") is True:
        return False
    return classification in {"bridge_leader_no_report", "bridge_leader_invalid_result", "bridge_leader_transport_failure"}


def _clone_packet_for_bridge_leader_retry(packet: dict[str, Any], attempt: int) -> dict[str, Any]:
    cloned = deepcopy(packet)
    binding = cloned.setdefault("binding", {})
    run_id = str(binding.get("run_id") or cloned.get("run_id") or "")
    old_binding = dict(binding)
    sub_session_id = f"sub_{uuid.uuid4().hex[:12]}"
    bridge_window_id = f"bw_{run_id}_{sub_session_id}" if run_id else f"bw_retry_{uuid.uuid4().hex[:12]}"
    team_id = f"team_{uuid.uuid4().hex[:12]}"
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    binding.update(
        {
            "sub_session_id": sub_session_id,
            "bridge_window_id": bridge_window_id,
            "parent_tool_use_id": f"tool_{uuid.uuid4().hex[:12]}",
            "team_id_or_null": team_id,
            "task_id_or_null": task_id,
            "bridge_leader_retry_attempt": attempt,
            "retry_source_bridge_window_id": old_binding.get("bridge_window_id"),
            "lifecycle_status": "bridge_call_intended",
            "closed_at": None,
        }
    )
    if isinstance(cloned.get("team_spec"), dict):
        cloned["team_spec"]["team_id_or_null"] = team_id
    if isinstance(cloned.get("task_spec"), dict):
        cloned["task_spec"]["task_id_or_null"] = task_id
    if isinstance(cloned.get("task_team_mapping"), dict):
        cloned["task_team_mapping"]["team_id_or_null"] = team_id
        cloned["task_team_mapping"]["task_id_or_null"] = task_id
    cloned["dispatch_contract"] = build_dispatch_contract(cloned)
    return cloned


def _bridge_leader_retry_attempt(attempt: int, packet: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    error = result.get("error_or_null") if isinstance(result.get("error_or_null"), dict) else {}
    binding = packet.get("binding", {}) if isinstance(packet.get("binding"), dict) else {}
    return {
        "attempt": attempt,
        "bridge_window_id": binding.get("bridge_window_id"),
        "sub_session_id": binding.get("sub_session_id"),
        "status": result.get("status"),
        "failure_stage_or_null": result.get("failure_stage_or_null"),
        "error_type": error.get("type"),
        "message": error.get("message"),
    }


def _with_bridge_leader_retry_evidence(result: dict[str, Any], attempts: list[dict[str, Any]], *, max_attempts: int) -> dict[str, Any]:
    if len(attempts) <= 1:
        return result
    updated = deepcopy(result)
    evidence = updated.get("evidence") if isinstance(updated.get("evidence"), dict) else {}
    updated["evidence"] = {
        **evidence,
        "bridge_leader_retry": {
            "attempts": attempts,
            "max_attempts": max_attempts,
            "final_attempt": len(attempts),
        },
    }
    return updated


def _record_bridge_leader_retry_scheduled(
    control_root: str | Path,
    source_packet: dict[str, Any],
    next_packet: dict[str, Any],
    result: dict[str, Any],
    *,
    attempt: int,
    max_attempts: int,
    delay_ms: int,
    original_bridge_window_id: str,
    runtime_runs_root: str | Path | None,
    persist: bool,
) -> None:
    binding = source_packet.get("binding", {})
    next_binding = next_packet.get("binding", {})
    error = result.get("error_or_null") if isinstance(result.get("error_or_null"), dict) else {}
    dispatch_workflow_event(
        control_root,
        {
            "run_id": binding.get("run_id"),
            "main_session_id": binding.get("main_session_id"),
            "sub_session_id": binding.get("sub_session_id"),
            "bridge_window_id": binding.get("bridge_window_id"),
            "agent_id": "runtime.retry",
            "agent_type": "runtime",
            "tool_name": "call_bridge_sdk",
            "tool_use_id": binding.get("parent_tool_use_id"),
            "event_kind": "retry_attempt_scheduled",
            "payload": {
                "retry_scope": "bridge_leader_execution",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "delay_ms": delay_ms,
                "source_bridge_window_id": binding.get("bridge_window_id"),
                "target_bridge_window_id": next_binding.get("bridge_window_id"),
                "original_bridge_window_id": original_bridge_window_id,
                "retry_action": {
                    "kind": "retry_bridge_sdk_call",
                    "requires_new_bridge_window": True,
                    "requires_same_packet": True,
                },
                "reason": {
                    "error_type": error.get("type"),
                    "message": error.get("message"),
                    "failure_stage_or_null": result.get("failure_stage_or_null"),
                },
            },
        },
        runtime_runs_root=runtime_runs_root,
        persist=persist,
    )


def _record_main_bridge_start(
    control_root: str | Path,
    packet: dict[str, Any],
    *,
    runtime_runs_root: str | Path | None,
    persist: bool,
) -> None:
    binding = packet["binding"]
    base = {
        "run_id": binding["run_id"],
        "main_session_id": binding["main_session_id"],
        "sub_session_id": binding["sub_session_id"],
        "bridge_window_id": binding["bridge_window_id"],
        "agent_id": binding.get("opened_by_agent_id") or "main-leader",
        "agent_type": "main-leader",
        "tool_name": "call_bridge_sdk",
        "tool_use_id": binding.get("parent_tool_use_id"),
        "payload": {"packet": packet},
    }
    for event_kind in ("bridge_call_intended", "pretooluse_allowed_by_main_leader", "call_bridge_sdk_started"):
        result = dispatch_workflow_event(
            control_root,
            {**base, "event_kind": event_kind},
            runtime_runs_root=runtime_runs_root,
            persist=persist,
        )
        if not result.ok:
            raise RuntimeError(f"{event_kind} rejected by runtime: {result.check_result.get('reasons')}")


def _record_main_bridge_error(
    control_root: str | Path,
    packet: dict[str, Any],
    *,
    runtime_runs_root: str | Path | None,
    persist: bool,
    error: dict[str, Any],
) -> None:
    binding = packet["binding"]
    dispatch_workflow_event(
        control_root,
        {
            "run_id": binding["run_id"],
            "main_session_id": binding["main_session_id"],
            "sub_session_id": binding["sub_session_id"],
            "bridge_window_id": binding["bridge_window_id"],
            "agent_id": binding.get("opened_by_agent_id") or "main-leader",
            "agent_type": "main-leader",
            "tool_name": "call_bridge_sdk",
            "tool_use_id": binding.get("parent_tool_use_id"),
            "event_kind": "call_bridge_sdk_error",
            "payload": {"packet": packet, "error_or_null": error},
        },
        runtime_runs_root=runtime_runs_root,
        persist=persist,
    )


def _record_main_bridge_interrupted(
    control_root: str | Path,
    packet: dict[str, Any],
    *,
    runtime_runs_root: str | Path | None,
    persist: bool,
    error: dict[str, Any],
) -> None:
    binding = packet["binding"]
    dispatch_workflow_event(
        control_root,
        {
            "run_id": binding["run_id"],
            "main_session_id": binding["main_session_id"],
            "sub_session_id": binding["sub_session_id"],
            "bridge_window_id": binding["bridge_window_id"],
            "agent_id": binding.get("opened_by_agent_id") or "main-leader",
            "agent_type": "main-leader",
            "tool_name": "call_bridge_sdk",
            "tool_use_id": binding.get("parent_tool_use_id"),
            "event_kind": "bridge_call_interrupted",
            "payload": {"packet": packet, "error_or_null": error, "interrupt_source": "manual_user_interrupt"},
        },
        runtime_runs_root=runtime_runs_root,
        persist=persist,
    )
