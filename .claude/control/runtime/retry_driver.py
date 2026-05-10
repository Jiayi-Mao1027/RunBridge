from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loader import ControlPaths, load_json_file, load_jsonl
from repo_runtime import get_repo_runtime_root


RETRY_ACTION_KINDS = {
    "repair_bridge_output",
    "retry_bridge_sdk_call",
    "continue_waiting",
    "poll_process",
    "retry_message_dispatch",
}


@dataclass(frozen=True, slots=True)
class RetryDriverDecision:
    ready: bool
    allowed: bool
    action_kind: str | None
    next_event_kind: str | None
    reason: str
    retry_event_id: str | None
    repo_key: str | None
    run_id: str | None
    bridge_window_id: str | None
    attempt: int | None
    max_attempts: int | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_scheduled_retry_events(
    control_root: str | Path,
    run_id: str,
    *,
    repo_key: str | None = None,
    runtime_runs_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    paths = _paths(control_root, repo_key=repo_key, runtime_runs_root=runtime_runs_root)
    return [
        record
        for record in load_jsonl(paths.run_root(run_id) / "event_log.jsonl")
        if record.get("event_kind") == "retry_attempt_scheduled"
    ]


def evaluate_retry_attempt(
    control_root: str | Path,
    retry_event: dict[str, Any],
    *,
    repo_key: str | None = None,
    runtime_runs_root: str | Path | None = None,
    now: datetime | None = None,
) -> RetryDriverDecision:
    payload = retry_event.get("payload") if isinstance(retry_event.get("payload"), dict) else {}
    event_repo_key = str(repo_key or retry_event.get("repo_key") or payload.get("repo_key") or "").strip() or None
    run_id = str(retry_event.get("run_id") or payload.get("run_id") or "").strip() or None
    bridge_window_id = str(retry_event.get("bridge_window_id") or payload.get("bridge_window_id") or "").strip() or None
    action = payload.get("retry_action") if isinstance(payload.get("retry_action"), dict) else {}
    action_kind = str(action.get("kind") or "").strip() or None
    attempt = _positive_int(payload.get("attempt"))
    max_attempts = _positive_int(payload.get("max_attempts"))
    if not run_id:
        return _decision(False, False, action_kind, None, "missing_run_id", retry_event, event_repo_key, run_id, bridge_window_id, attempt, max_attempts)
    if action_kind not in RETRY_ACTION_KINDS:
        return _decision(False, False, action_kind, None, "missing_or_unknown_retry_action", retry_event, event_repo_key, run_id, bridge_window_id, attempt, max_attempts)
    if action.get("allowed") is not True:
        return _decision(False, False, action_kind, "enter_anomaly", "retry_action_not_allowed", retry_event, event_repo_key, run_id, bridge_window_id, attempt, max_attempts)
    if max_attempts is not None and max_attempts > 0 and attempt is not None and attempt > max_attempts:
        return _decision(True, False, action_kind, "enter_anomaly", "retry_attempt_exhausted", retry_event, event_repo_key, run_id, bridge_window_id, attempt, max_attempts)
    if not _delay_elapsed(payload, now=now):
        return _decision(False, True, action_kind, None, "retry_delay_not_elapsed", retry_event, event_repo_key, run_id, bridge_window_id, attempt, max_attempts)
    if action.get("requires_same_packet") is True:
        boundary = check_same_packet_boundary(control_root, retry_event, repo_key=event_repo_key, runtime_runs_root=runtime_runs_root)
        if not boundary.get("valid"):
            return _decision(True, False, action_kind, "enter_anomaly", str(boundary.get("reason") or "same_packet_boundary_failed"), retry_event, event_repo_key, run_id, bridge_window_id, attempt, max_attempts)
    return _decision(True, True, action_kind, _next_event_for_action(action_kind), "ready", retry_event, event_repo_key, run_id, bridge_window_id, attempt, max_attempts)


def check_same_packet_boundary(
    control_root: str | Path,
    retry_event: dict[str, Any],
    *,
    repo_key: str | None = None,
    runtime_runs_root: str | Path | None = None,
) -> dict[str, Any]:
    payload = retry_event.get("payload") if isinstance(retry_event.get("payload"), dict) else {}
    expected_hash = str(payload.get("packet_hash") or "").strip()
    run_id = str(retry_event.get("run_id") or payload.get("run_id") or "").strip()
    if not expected_hash:
        return {"valid": False, "reason": "missing_retry_packet_hash"}
    if not run_id:
        return {"valid": False, "reason": "missing_run_id"}
    paths = _paths(control_root, repo_key=repo_key, runtime_runs_root=runtime_runs_root)
    latest = _latest_packet_hash(paths.run_root(run_id))
    if latest is None:
        return {"valid": False, "reason": "no_packet_hash_observed"}
    return {"valid": latest == expected_hash, "reason": "ok" if latest == expected_hash else "packet_hash_mismatch", "expected_packet_hash": expected_hash, "latest_packet_hash": latest}


def dispatch_retry_action_stub(decision: RetryDriverDecision) -> dict[str, Any]:
    """Contract placeholder.

    The driver is intentionally not enabled in Beta2. Callers can use this
    return value to decide whether to dispatch the next event themselves.
    """
    return {
        "enabled": False,
        "reason": "retry_driver_not_enabled",
        "decision": decision.as_dict(),
    }


def _paths(control_root: str | Path, *, repo_key: str | None, runtime_runs_root: str | Path | None) -> ControlPaths:
    if repo_key and runtime_runs_root is None:
        runtime_runs_root = get_repo_runtime_root(control_root, repo_key)
    return ControlPaths.from_root(control_root, runtime_runs_root)


def _delay_elapsed(payload: dict[str, Any], *, now: datetime | None = None) -> bool:
    delay_ms = _positive_int(payload.get("delay_ms")) or 0
    if delay_ms <= 0:
        return True
    timestamp = _parse_iso(payload.get("timestamp"))
    if timestamp is None:
        return False
    current = now or datetime.now(timezone.utc)
    return (current - timestamp).total_seconds() * 1000 >= delay_ms


def _latest_packet_hash(run_root: Path) -> str | None:
    for record in reversed(load_jsonl(run_root / "event_log.jsonl")):
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        packet = payload.get("packet") if isinstance(payload.get("packet"), dict) else None
        if packet:
            from retry_policy import packet_hash

            return packet_hash(packet)
    return None


def _next_event_for_action(action_kind: str) -> str:
    return {
        "repair_bridge_output": "retry_artifact_collection",
        "retry_bridge_sdk_call": "bridge_call_intended",
        "continue_waiting": "continue_waiting",
        "poll_process": "continue_waiting",
        "retry_message_dispatch": "message_dispatch_retry_started",
    }[action_kind]


def _decision(
    ready: bool,
    allowed: bool,
    action_kind: str | None,
    next_event_kind: str | None,
    reason: str,
    retry_event: dict[str, Any],
    repo_key: str | None,
    run_id: str | None,
    bridge_window_id: str | None,
    attempt: int | None,
    max_attempts: int | None,
) -> RetryDriverDecision:
    return RetryDriverDecision(
        ready=ready,
        allowed=allowed,
        action_kind=action_kind,
        next_event_kind=next_event_kind,
        reason=reason,
        retry_event_id=str(retry_event.get("event_id") or "") or None,
        repo_key=repo_key,
        run_id=run_id,
        bridge_window_id=bridge_window_id,
        attempt=attempt,
        max_attempts=max_attempts,
    )


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
