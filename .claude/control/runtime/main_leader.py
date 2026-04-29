from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import uuid

from loader import ControlPaths, load_json_file
from workflow_runtime import SCHEMA_VERSION, build_runtime_snapshot


PACKET_SCHEMA_VERSION = "0.1"
DEFAULT_BRIDGE_ACTIONS = ["team_create", "task_create", "send_messages", "task_complete", "team_delete"]


def read_runtime_snapshot(
    control_root: str | Path,
    run_id: str,
    *,
    runtime_runs_root: str | Path | None = None,
) -> dict[str, Any]:
    paths = ControlPaths.from_root(control_root, runtime_runs_root)
    run_ledger = load_json_file(paths.run_ledger_path(run_id), default={}) or {}
    if not run_ledger:
        now = _now_iso()
        run_ledger = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "main_session_id": run_id,
            "workflow_name": "bridge_window_workflow",
            "workflow_version": SCHEMA_VERSION,
            "run_status": "in_progress",
            "current_phase": "leader_freeze",
            "created_at": now,
            "updated_at": now,
            "closed_at": None,
        }
    return build_runtime_snapshot(paths, run_ledger)


def decide_next_bridge_packet(
    control_root: str | Path,
    run_id: str,
    *,
    runtime_runs_root: str | Path | None = None,
    main_session_id: str | None = None,
    user_instruction: str | None = None,
    task_spec: dict[str, Any] | None = None,
    team_spec: dict[str, Any] | None = None,
    completion_contract: dict[str, Any] | None = None,
    report_contract: dict[str, Any] | None = None,
    target_phase: str | None = None,
    phase_route: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    approval_requirements: list[dict[str, Any]] | None = None,
    expires_in_seconds: int | None = None,
) -> dict[str, Any]:
    snapshot = read_runtime_snapshot(control_root, run_id, runtime_runs_root=runtime_runs_root)
    return build_bridge_instruction_packet_for_this_invoke(
        snapshot=snapshot,
        main_session_id=main_session_id,
        user_instruction=user_instruction,
        task_spec=task_spec,
        team_spec=team_spec,
        completion_contract=completion_contract,
        report_contract=report_contract,
        target_phase=target_phase,
        phase_route=phase_route,
        allowed_tools=allowed_tools,
        approval_requirements=approval_requirements,
        expires_in_seconds=expires_in_seconds,
    )


def build_bridge_instruction_packet_for_this_invoke(
    *,
    snapshot: dict[str, Any],
    main_session_id: str | None = None,
    user_instruction: str | None = None,
    task_spec: dict[str, Any] | None = None,
    team_spec: dict[str, Any] | None = None,
    completion_contract: dict[str, Any] | None = None,
    report_contract: dict[str, Any] | None = None,
    target_phase: str | None = None,
    phase_route: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    approval_requirements: list[dict[str, Any]] | None = None,
    expires_in_seconds: int | None = None,
) -> dict[str, Any]:
    if snapshot.get("integrity", {}).get("has_hard_stop"):
        raise ValueError("cannot build bridge packet while hard_stop is active")
    if snapshot.get("integrity", {}).get("awaiting_approval"):
        raise ValueError("cannot build bridge packet while approval is pending")
    if "call_bridge_sdk" not in snapshot.get("allowed_actions", []):
        raise ValueError("current runtime snapshot does not allow call_bridge_sdk")

    run_id = str(snapshot["run_id"])
    resolved_main_session_id = str(main_session_id or snapshot.get("main_session_id") or run_id)
    sub_session_id = f"sub_{uuid.uuid4().hex[:12]}"
    bridge_window_id = f"bw_{run_id}_{sub_session_id}"
    parent_tool_use_id = f"tool_{uuid.uuid4().hex[:12]}"
    now = _now_iso()

    resolved_target_phase = _resolve_target_phase(snapshot, target_phase)
    resolved_route = phase_route or _resolve_phase_route(snapshot, resolved_target_phase)
    resolved_completion = completion_contract or _default_completion_contract()
    resolved_report = report_contract or _default_report_contract()
    resolved_team = _normalize_team_spec(team_spec, allowed_tools or [])
    resolved_task = _normalize_task_spec(
        task_spec,
        user_instruction=user_instruction,
        target_phase=resolved_target_phase,
        completion_contract=resolved_completion,
        report_contract=resolved_report,
    )
    mapping = _build_task_team_mapping(resolved_task, resolved_team)

    binding = {
        "run_id": run_id,
        "main_session_id": resolved_main_session_id,
        "sub_session_id": sub_session_id,
        "bridge_window_id": bridge_window_id,
        "parent_tool_use_id": parent_tool_use_id,
        "opened_by_agent_id": "main-leader",
        "opened_by_agent_type": "main-leader",
        "bridge_leader_id_or_null": None,
        "team_id_or_null": resolved_team.get("team_id_or_null"),
        "task_id_or_null": resolved_task.get("task_id_or_null"),
        "lifecycle_status": "bridge_call_intended",
        "created_at": now,
        "updated_at": now,
        "closed_at": None,
    }

    packet = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "binding": binding,
        "frozen_semantics": deepcopy(snapshot.get("semantic", {}).get("frozen") or {}),
        "frozen_scope": deepcopy(snapshot.get("scope", {}).get("frozen") or {}),
        "phase_route": resolved_route,
        "target_phase": resolved_target_phase,
        "team_spec": resolved_team,
        "task_spec": resolved_task,
        "task_team_mapping": mapping,
        "completion_contract": resolved_completion,
        "report_contract": resolved_report,
        "allowed_actions": list(DEFAULT_BRIDGE_ACTIONS),
        "allowed_tools": list(allowed_tools or []),
        "approval_requirements": list(approval_requirements or []),
        "created_at": now,
        "expires_at": _expiry(now, expires_in_seconds),
    }
    return packet


def _resolve_target_phase(snapshot: dict[str, Any], requested: str | None) -> str:
    if requested:
        return requested
    route = snapshot.get("route", {})
    if route.get("target_phase"):
        return str(route["target_phase"])
    allowed = snapshot.get("allowed_routes") or []
    if allowed:
        return str(allowed[0])
    return str(snapshot.get("current_phase") or "leader_freeze")


def _resolve_phase_route(snapshot: dict[str, Any], target_phase: str) -> list[str]:
    route = snapshot.get("route", {}).get("current_route")
    if isinstance(route, list) and route:
        return [str(item) for item in route]
    current = str(snapshot.get("current_phase") or "leader_freeze")
    return [current] if current == target_phase else [current, target_phase]


def _normalize_team_spec(team_spec: dict[str, Any] | None, allowed_tools: list[str]) -> dict[str, Any]:
    source = deepcopy(team_spec or {})
    teammates = source.get("teammate_specs")
    if not isinstance(teammates, list) or not teammates:
        teammates = [
            {
                "teammate_id_or_null": None,
                "teammate_name": "bridge-worker",
                "role": "execute",
                "allowed_tools": list(allowed_tools),
                "responsibilities": ["execute the single bridge-window task and report evidence"],
            }
        ]
    ownership = source.get("ownership_boundary")
    if not isinstance(ownership, dict):
        ownership = {
            "readable_scopes": [],
            "writable_scopes": [],
            "process_ownership_rules": ["only manage processes launched inside this bridge window"],
            "forbidden_actions": [],
        }
    return {
        "team_id_or_null": source.get("team_id_or_null"),
        "team_name": str(source.get("team_name") or "bridge-team"),
        "teammate_specs": teammates,
        "ownership_boundary": ownership,
    }


def _normalize_task_spec(
    task_spec: dict[str, Any] | None,
    *,
    user_instruction: str | None,
    target_phase: str,
    completion_contract: dict[str, Any],
    report_contract: dict[str, Any],
) -> dict[str, Any]:
    source = deepcopy(task_spec or {})
    subject = str(source.get("task_subject") or source.get("subject") or "bridge-window task")
    description = str(source.get("task_description") or source.get("description") or user_instruction or subject)
    return {
        "task_id_or_null": source.get("task_id_or_null") or source.get("task_id"),
        "task_subject": subject,
        "task_description": description,
        "task_kind": str(source.get("task_kind") or "bridge_window_task"),
        "target_phase": str(source.get("target_phase") or target_phase),
        "completion_contract": deepcopy(source.get("completion_contract") or completion_contract),
        "report_contract": deepcopy(source.get("report_contract") or report_contract),
    }


def _build_task_team_mapping(task_spec: dict[str, Any], team_spec: dict[str, Any]) -> dict[str, Any]:
    assignments = []
    for teammate in team_spec.get("teammate_specs", []):
        name = str(teammate.get("teammate_name") or "bridge-worker")
        assignments.append(
            {
                "teammate_id_or_null": teammate.get("teammate_id_or_null"),
                "assignment": f"{name}: {task_spec['task_description']}",
                "expected_output": "completion report and declared artifact refs",
            }
        )
    return {
        "task_id_or_null": task_spec.get("task_id_or_null"),
        "team_id_or_null": team_spec.get("team_id_or_null"),
        "teammate_assignments": assignments,
    }


def _default_completion_contract() -> dict[str, Any]:
    return {
        "required_outputs": ["report"],
        "required_artifacts": [],
        "validation_requirements": [],
        "success_criteria": ["bridge leader collected a report from the team"],
        "allowed_partial_result": True,
        "timeout_policy": {
            "heartbeat_interval_seconds": 60,
            "soft_timeout_seconds": 900,
            "hard_timeout_seconds": 3600,
            "timeout_action": "ask_main_leader",
        },
    }


def _default_report_contract() -> dict[str, Any]:
    return {
        "required_sections": ["summary", "evidence"],
        "required_evidence": ["runtime event ids"],
        "artifact_reporting_format": "list",
        "include_failure_reason": True,
        "include_next_action_recommendation": True,
    }


def _expiry(created_at: str, expires_in_seconds: int | None) -> str | None:
    if expires_in_seconds is None:
        return None
    created = datetime.fromisoformat(created_at)
    return (created + timedelta(seconds=expires_in_seconds)).isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
