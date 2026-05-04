from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
import uuid

from loader import ControlPaths, load_json_file
from workflow_runtime import SCHEMA_VERSION, build_runtime_snapshot


PACKET_SCHEMA_VERSION = "0.1"
DEFAULT_BRIDGE_ACTIONS = ["team_create", "task_create", "send_messages", "task_complete", "team_delete"]
READ_ONLY_TOOLS = ["Read", "Grep", "Glob", "LS"]
RESEARCH_TOOLS = [*READ_ONLY_TOOLS, "WebSearch", "WebFetch"]
READ_CHECK_TOOLS = [*READ_ONLY_TOOLS, "Bash"]
WRITE_TOOLS = [*READ_ONLY_TOOLS, "Bash", "Edit", "Write"]
DEFAULT_BRIDGE_LEADER_TOOLS = ["Agent", *WRITE_TOOLS]
PHASE_BRIDGE_TOOLS = {
    "l2_advisory": ["Agent", *RESEARCH_TOOLS],
    "l3_bridge": DEFAULT_BRIDGE_LEADER_TOOLS,
    "l4_implement": DEFAULT_BRIDGE_LEADER_TOOLS,
    "l4_execute": ["Agent", "Read", "Grep", "Glob", "LS", "Bash", "Write"],
    "l4_anomaly": ["Agent", *READ_CHECK_TOOLS],
}
DEFAULT_FORBIDDEN_ACTIONS = [
    "destructive filesystem operations outside writable scopes",
    "external network calls unless explicitly approved",
    "dependency installation unless explicitly approved",
]
PHASE_OWNERSHIP_DEFAULTS = {
    "l2_advisory": {"readable_scopes": ["."], "writable_scopes": []},
    "l3_bridge": {"readable_scopes": ["."], "writable_scopes": ["README.md", "docs/", "*.md"]},
    "l4_implement": {"readable_scopes": ["."], "writable_scopes": ["."]},
    "l4_execute": {"readable_scopes": ["."], "writable_scopes": ["."]},
    "l4_anomaly": {"readable_scopes": ["."], "writable_scopes": []},
}

PHASE_TEAM_DEFAULTS = {
    "l2_advisory": [
        ("chiefmate-a", "advisory", RESEARCH_TOOLS, "produce upstream interpretation, assumptions, plan critique, and peer-aware advisory judgment"),
        ("chiefmate-b", "advisory", RESEARCH_TOOLS, "produce independent upstream advisory judgment and critique chiefmate-a when relevant"),
    ],
    "l3_bridge": [
        ("curator", "artifact_curation", WRITE_TOOLS, "clarify active logs, datasets, checkpoints, outputs, archive boundaries, and traceability"),
        ("preflight-initial", "preflight_audit", READ_CHECK_TOOLS, "inspect implementation-facing repo/config state and surface required changes before implementation"),
        ("refresher", "documentation_refresh", ["Read", "Grep", "Glob", "LS", "Edit", "Write"], "refresh bounded human-facing repository documentation when needed"),
    ],
    "l4_implement": [
        ("implementor", "implement", WRITE_TOOLS, "make approved code/config changes and collect bounded validation evidence"),
        ("rungater", "implementation_gate", READ_CHECK_TOOLS, "judge post-implementation readiness and recommend proceed, repair, reroute, or stop"),
    ],
    "l4_execute": [
        ("executor", "formal_execute", ["Read", "Grep", "Glob", "LS", "Bash", "Write"], "run the approved workflow and record exact execution evidence"),
        ("postrun", "postrun_audit", READ_CHECK_TOOLS, "audit execution artifacts, classify outcome, and recommend anomaly routing when needed"),
    ],
    "l4_anomaly": [
        ("anomaly-analyst-a", "anomaly_analysis", READ_CHECK_TOOLS, "build evidence-backed anomaly hypotheses and discriminative next checks"),
        ("anomaly-analyst-b", "anomaly_analysis", READ_CHECK_TOOLS, "build independent anomaly hypotheses and critique anomaly-analyst-a when relevant"),
    ],
}


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
    target_phase: str | None = None,
) -> dict[str, Any]:
    snapshot = read_runtime_snapshot(control_root, run_id, runtime_runs_root=runtime_runs_root)
    return build_bridge_instruction_packet_for_this_invoke(
        snapshot=snapshot,
        main_session_id=main_session_id,
        user_instruction=user_instruction,
        task_spec=task_spec,
        target_phase=target_phase,
    )


def build_bridge_instruction_packet_for_this_invoke(
    *,
    snapshot: dict[str, Any],
    main_session_id: str | None = None,
    user_instruction: str | None = None,
    task_spec: dict[str, Any] | None = None,
    target_phase: str | None = None,
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
    resolved_route = _resolve_phase_route(snapshot, resolved_target_phase)
    resolved_completion = _default_completion_contract()
    resolved_report = _default_report_contract()
    bridge_allowed_tools = _default_bridge_tools(resolved_target_phase)
    resolved_team = _normalize_team_spec(target_phase=resolved_target_phase)
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
        "allowed_tools": list(bridge_allowed_tools),
        "approval_requirements": [],
        "created_at": now,
        "expires_at": None,
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
    if isinstance(route, list) and route and str(route[-1]) == target_phase:
        return [str(item) for item in route]
    current = str(snapshot.get("current_phase") or "leader_freeze")
    return [current] if current == target_phase else [current, target_phase]


def _normalize_team_spec(
    *,
    target_phase: str,
) -> dict[str, Any]:
    if target_phase in PHASE_TEAM_DEFAULTS:
        teammates = _default_teammate_specs(target_phase)
        ownership = _default_ownership_boundary(target_phase)
    else:
        teammates = _default_teammate_specs(target_phase)
        ownership = _default_ownership_boundary(target_phase)
    return {
        "team_id_or_null": None,
        "team_name": f"bridge-{target_phase}-team",
        "teammate_specs": teammates,
        "ownership_boundary": ownership,
    }


def _default_bridge_tools(target_phase: str) -> list[str]:
    return list(PHASE_BRIDGE_TOOLS.get(target_phase, DEFAULT_BRIDGE_LEADER_TOOLS))


def _default_ownership_boundary(target_phase: str) -> dict[str, Any]:
    scopes = PHASE_OWNERSHIP_DEFAULTS.get(target_phase, {"readable_scopes": ["."], "writable_scopes": []})
    return {
        "readable_scopes": list(scopes["readable_scopes"]),
        "writable_scopes": list(scopes["writable_scopes"]),
        "process_ownership_rules": ["only manage processes launched inside this bridge window"],
        "forbidden_actions": list(DEFAULT_FORBIDDEN_ACTIONS),
    }


def _default_teammate_specs(target_phase: str) -> list[dict[str, Any]]:
    defaults = PHASE_TEAM_DEFAULTS.get(target_phase)
    if not defaults:
        return [
            {
                "teammate_id_or_null": None,
                "teammate_name": "bridge-worker",
                "role": "execute",
                "allowed_tools": list(READ_CHECK_TOOLS),
                "responsibilities": ["execute the single bridge-window task and report evidence"],
            }
        ]

    specs = []
    for name, role, default_tools, responsibility in defaults:
        specs.append(
            {
                "teammate_id_or_null": None,
                "teammate_name": name,
                "role": role,
                "allowed_tools": list(default_tools),
                "responsibilities": [responsibility],
            }
        )
    return specs


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
        "target_phase": target_phase,
        "completion_contract": deepcopy(completion_contract),
        "report_contract": deepcopy(report_contract),
    }


def _build_task_team_mapping(task_spec: dict[str, Any], team_spec: dict[str, Any]) -> dict[str, Any]:
    assignments = []
    ownership = team_spec.get("ownership_boundary", {}) if isinstance(team_spec.get("ownership_boundary"), dict) else {}
    for teammate in team_spec.get("teammate_specs", []):
        name = str(teammate.get("teammate_name") or "bridge-worker")
        responsibilities = teammate.get("responsibilities") if isinstance(teammate.get("responsibilities"), list) else []
        assignments.append(
            {
                "teammate_id_or_null": teammate.get("teammate_id_or_null"),
                "assignment": "\n".join(
                    [
                        f"{name}: {task_spec['task_description']}",
                        f"Role: {teammate.get('role') or 'bridge teammate'}",
                        f"Responsibilities: {_json_list(responsibilities)}",
                        f"Allowed tools: {_json_list(teammate.get('allowed_tools'))}",
                        f"Readable scopes: {_json_list(ownership.get('readable_scopes'))}",
                        f"Writable scopes: {_json_list(ownership.get('writable_scopes'))}",
                        f"Completion contract: {_json_dict(task_spec.get('completion_contract'))}",
                        f"Report contract: {_json_dict(task_spec.get('report_contract'))}",
                        "Do not read .claude/runtime_state/bridge_prompts for task context; that bridge prompt artifact is for audit only.",
                        "When using Read, omit optional parameters you do not need. Never pass pages as an empty string.",
                    ]
                ),
                "expected_output": "completion report and declared artifact refs",
            }
        )
    return {
        "task_id_or_null": task_spec.get("task_id_or_null"),
        "team_id_or_null": team_spec.get("team_id_or_null"),
        "teammate_assignments": assignments,
    }


def _json_list(value: Any) -> str:
    items = value if isinstance(value, list) else []
    return json.dumps([str(item) for item in items], ensure_ascii=False)


def _json_dict(value: Any) -> str:
    data = value if isinstance(value, dict) else {}
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
