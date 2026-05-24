from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any
import uuid

from loader import ControlPaths, load_json_file
from policy_compiler import compile_policy
from repo_runtime import get_repo_runtime_root
from team_planner import RiskBasedTeamSelector
from workflow_runtime import SCHEMA_VERSION, build_runtime_snapshot
from dispatch_contract import build_agent_dispatch, build_dispatch_contract


PACKET_SCHEMA_VERSION = "0.1"
DEFAULT_BRIDGE_ACTIONS = ["team_create", "task_create", "send_messages", "task_complete", "team_delete"]
READ_ONLY_TOOLS = ["Read", "Grep", "Glob", "LS"]
RESEARCH_TOOLS = [*READ_ONLY_TOOLS, "WebSearch", "WebFetch"]
READ_CHECK_TOOLS = [*READ_ONLY_TOOLS, "Bash"]
ANOMALY_TOOLS = [*READ_CHECK_TOOLS, "WebSearch", "WebFetch"]
L3_WRITE_TOOLS = [*READ_ONLY_TOOLS, "Edit", "Write"]
L3_CURATOR_TOOLS = [*READ_ONLY_TOOLS, "Bash", "Edit", "Write"]
WRITE_TOOLS = [*READ_ONLY_TOOLS, "Bash", "Edit", "Write"]
DEFAULT_BRIDGE_LEADER_TOOLS = ["Agent", *WRITE_TOOLS]
PHASE_BRIDGE_TOOLS = {
    "l2_advisory": ["Agent", *RESEARCH_TOOLS],
    "l3_bridge": ["Agent", *L3_CURATOR_TOOLS],
    "l4_implement": DEFAULT_BRIDGE_LEADER_TOOLS,
    "l4_execute": ["Agent", "Read", "Grep", "Glob", "LS", "Bash", "Write"],
    "l4_anomaly": ["Agent", *ANOMALY_TOOLS],
}
DEFAULT_FORBIDDEN_ACTIONS = [
    "destructive filesystem operations outside writable scopes",
    "external network calls through Bash or project code that are unrelated to the accepted packet, require secrets/tokens/payment/manual license acceptance, or expose private data; task-authorized public no-token acquisition and task-relevant research are allowed when the phase/tools permit them",
    "destructive or global dependency/environment changes unless explicitly approved; task-scoped dependency repair, version pinning, cache rebuilds, or local tooling bypasses are allowed in L4 implement/execute when needed and auditable",
    "implementation content edits during L3 artifact curation unless the file is human-facing documentation already in L3 doc scope",
    "physical deletion of user/project artifacts unless the item is clearly regenerable trash, an empty duplicate, or explicitly approved",
]
PHASE_ACTIVE_SURFACE_POLICIES: dict[str, list[str]] = {}
PHASE_OWNERSHIP_DEFAULTS = {
    "l2_advisory": {"readable_scopes": ["."], "writable_scopes": []},
    "l3_bridge": {"readable_scopes": ["."], "writable_scopes": ["."]},
    "l4_implement": {"readable_scopes": ["."], "writable_scopes": ["."]},
    "l4_execute": {"readable_scopes": ["."], "writable_scopes": ["."]},
    "l4_anomaly": {"readable_scopes": ["."], "writable_scopes": []},
}

DEFAULT_TIMEOUT_POLICY = {
    "heartbeat_interval_seconds": 60,
    "soft_timeout_seconds": 900,
    "hard_timeout_seconds": 3600,
    "timeout_action": "ask_main_leader",
}

PHASE_TIMEOUT_POLICY = {
    "l4_execute": {
        "heartbeat_interval_seconds": 120,
        "soft_timeout_seconds": 21600,
        "hard_timeout_seconds": 86400,
        "executor_hard_timeout_disabled": True,
        "timeout_action": "ask_main_leader",
        "wait_until_process_complete": True,
        "partial_return_allowed_only_after_process_terminal": True,
    },
}

PHASE_TEAM_DEFAULTS = {
    "l2_advisory": [
        ("chiefmate-a", "advisory", RESEARCH_TOOLS, "produce upstream interpretation, assumptions, plan critique, peer challenges, and confidence-loop advisory judgment"),
        ("chiefmate-b", "advisory", RESEARCH_TOOLS, "produce independent upstream advisory judgment, critique chiefmate-a/chiefmate-c, and challenge weak convergence"),
        ("chiefmate-c", "advisory", RESEARCH_TOOLS, "produce additional GPT-main peer critique, challenge chiefmate-a/chiefmate-b, and run confidence-loop validation"),
    ],
    "l3_bridge": [
        ("curator", "artifact_curation", L3_CURATOR_TOOLS, "clarify active logs, datasets, checkpoints, outputs, archive boundaries, and traceability; use Bash only for bounded filesystem curation"),
        ("preflight-initial", "preflight_audit", READ_ONLY_TOOLS, "inspect implementation-facing repo/config state and surface required changes before implementation without running commands"),
        ("refresher", "documentation_refresh", ["Read", "Grep", "Glob", "LS", "Edit", "Write"], "refresh CLAUDE.md and bounded human-facing repository documentation when needed"),
    ],
    "l4_implement": [
        ("implementor", "implement", WRITE_TOOLS, "make approved code/config changes and collect bounded validation evidence"),
        ("rungater", "implementation_gate", READ_CHECK_TOOLS, "judge post-implementation readiness and recommend proceed, repair, reroute, or stop"),
    ],
    "l4_execute": [
        ("executor", "formal_execute", ["Read", "Grep", "Glob", "LS", "Bash", "Write"], "run the approved workflow and record exact execution evidence"),
        ("postrun", "postrun_audit", READ_CHECK_TOOLS, "audit execution artifacts, terminal status, outcome classification, and recommend anomaly routing when needed"),
    ],
    "l4_anomaly": [
        ("anomaly-analyst-a", "anomaly_analysis", ANOMALY_TOOLS, "perform a complete independent anomaly diagnosis before peer review: inspect local evidence, answer-level/result samples when relevant, causal alternatives, missing evidence, and discriminative next checks"),
        ("anomaly-analyst-b", "anomaly_analysis", ANOMALY_TOOLS, "perform a complete independent anomaly diagnosis before peer review: inspect local evidence, answer-level/result samples when relevant, causal alternatives, missing evidence, and discriminative next checks"),
        ("anomaly-analyst-c", "anomaly_analysis", ANOMALY_TOOLS, "perform a complete independent anomaly diagnosis before peer review: inspect local evidence, answer-level/result samples when relevant, causal alternatives, missing evidence, and discriminative next checks"),
    ],
}


def read_runtime_snapshot(
    control_root: str | Path,
    run_id: str,
    *,
    repo_key: str | None = None,
    runtime_runs_root: str | Path | None = None,
    allow_synthetic: bool = False,
) -> dict[str, Any]:
    if repo_key and runtime_runs_root is None:
        runtime_runs_root = get_repo_runtime_root(control_root, repo_key)
    paths = ControlPaths.from_root(control_root, runtime_runs_root)
    run_ledger = load_json_file(paths.run_ledger_path(run_id), default={}) or {}
    synthetic = False
    if not run_ledger:
        if not allow_synthetic:
            raise FileNotFoundError(f"runtime ledger not found for run_id={run_id}")
        synthetic = True
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
    snapshot = build_runtime_snapshot(paths, run_ledger)
    if synthetic:
        snapshot["synthetic"] = True
        snapshot["source"] = "fallback_no_ledger"
    return snapshot


def decide_next_bridge_packet(
    control_root: str | Path,
    run_id: str,
    *,
    repo_key: str | None = None,
    runtime_runs_root: str | Path | None = None,
    main_session_id: str | None = None,
    user_instruction: str | None = None,
    task_spec: dict[str, Any] | None = None,
    target_phase: str | None = None,
) -> dict[str, Any]:
    snapshot = read_runtime_snapshot(control_root, run_id, repo_key=repo_key, runtime_runs_root=runtime_runs_root)
    phase_contracts = load_phase_contracts(control_root)
    return build_bridge_instruction_packet_for_this_invoke(
        snapshot=snapshot,
        main_session_id=main_session_id,
        user_instruction=user_instruction,
        task_spec=task_spec,
        target_phase=target_phase,
        phase_contracts=phase_contracts,
    )


def load_phase_contracts(control_root: str | Path) -> dict[str, Any]:
    return compile_policy(control_root).phase_contracts


def build_bridge_instruction_packet_for_this_invoke(
    *,
    snapshot: dict[str, Any],
    main_session_id: str | None = None,
    user_instruction: str | None = None,
    task_spec: dict[str, Any] | None = None,
    target_phase: str | None = None,
    phase_contracts: dict[str, Any] | None = None,
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
    contracts = phase_contracts if isinstance(phase_contracts, dict) else {}
    resolved_completion = _default_completion_contract(resolved_target_phase, contracts)
    resolved_report = _default_report_contract(resolved_target_phase, contracts)
    bridge_allowed_tools = _default_bridge_tools(resolved_target_phase, contracts)
    resolved_team = _normalize_team_spec(target_phase=resolved_target_phase, phase_contracts=contracts)
    resolved_task = _normalize_task_spec(
        _attach_runtime_followup_context(task_spec, snapshot),
        user_instruction=user_instruction,
        target_phase=resolved_target_phase,
        completion_contract=resolved_completion,
        report_contract=resolved_report,
        phase_contracts=contracts,
    )
    team_planning = _plan_team_for_task(resolved_target_phase, resolved_task, resolved_team, contracts)
    resolved_team = team_planning["team_spec"]
    team_id = str(resolved_team.get("team_id_or_null") or f"team_{uuid.uuid4().hex[:12]}")
    task_id = str(resolved_task.get("task_id_or_null") or f"task_{uuid.uuid4().hex[:12]}")
    resolved_team["team_id_or_null"] = team_id
    resolved_task["task_id_or_null"] = task_id
    mapping = _build_task_team_mapping(resolved_task, resolved_team, phase_contracts=contracts)
    mapping["team_id_or_null"] = team_id
    mapping["task_id_or_null"] = task_id

    binding = {
        "repo_key": snapshot.get("repo_key"),
        "run_id": run_id,
        "main_session_id": resolved_main_session_id,
        "sub_session_id": sub_session_id,
        "bridge_window_id": bridge_window_id,
        "parent_tool_use_id": parent_tool_use_id,
        "opened_by_agent_id": "main-leader",
        "opened_by_agent_type": "main-leader",
        "bridge_leader_id_or_null": None,
        "team_id_or_null": team_id,
        "task_id_or_null": task_id,
        "lifecycle_status": "bridge_call_intended",
        "created_at": now,
        "updated_at": now,
        "closed_at": None,
    }

    packet = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "policy_contract_ref": _policy_contract_ref(contracts),
        "repo_key": snapshot.get("repo_key"),
        "binding": binding,
        "frozen_semantics": deepcopy(snapshot.get("semantic", {}).get("frozen") or {}),
        "frozen_scope": deepcopy(snapshot.get("scope", {}).get("frozen") or {}),
        "phase_route": resolved_route,
        "target_phase": resolved_target_phase,
        "team_spec": resolved_team,
        "team_planning": team_planning["decision"],
        "task_spec": resolved_task,
        "task_team_mapping": mapping,
        "completion_contract": resolved_completion,
        "report_contract": resolved_report,
        "retry_policies": deepcopy(contracts.get("retry_policies") or {}) if isinstance(contracts.get("retry_policies"), dict) else {},
        "allowed_actions": _contracts_list(contracts, "default_bridge_actions") or list(DEFAULT_BRIDGE_ACTIONS),
        "allowed_tools": list(bridge_allowed_tools),
        "approval_requirements": [],
        "created_at": now,
        "expires_at": None,
    }
    packet["dispatch_contract"] = build_dispatch_contract(packet)
    return packet


BLOCKING_REPORT_CLASSIFICATIONS = {
    "hard_stop",
    "blocked",
    "execution_blocked",
    "readiness_blocked",
    "dependency_blocked",
    "execution_defect",
}


def _attach_runtime_followup_context(task_spec: dict[str, Any] | None, snapshot: dict[str, Any]) -> dict[str, Any]:
    source = deepcopy(task_spec or {})
    context = _latest_blocking_followup_context(snapshot)
    if not context:
        return source
    existing = source.get("runtime_followup_context")
    if not isinstance(existing, dict):
        existing = {}
    else:
        existing = deepcopy(existing)
    existing["latest_bridge_result"] = context
    source["runtime_followup_context"] = existing
    return source


def _latest_blocking_followup_context(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    latest = snapshot.get("last_bridge_result")
    if not isinstance(latest, dict):
        return None
    status = str(latest.get("status") or "").strip()
    reports = _latest_reports(latest)
    blocking_reports = [report for report in reports if _report_is_blocking(report)]
    error = latest.get("error_or_null") if isinstance(latest.get("error_or_null"), dict) else {}
    blocked_teammates = error.get("blocked_teammates") if isinstance(error.get("blocked_teammates"), list) else []
    if status not in {"failed", "partial_or_failed"} and not blocking_reports and not blocked_teammates:
        return None
    bridge_window_id = str(latest.get("bridge_window_id") or "")
    return {
        "bridge_window_id": bridge_window_id or None,
        "status": status or None,
        "failure_stage_or_null": latest.get("failure_stage_or_null"),
        "target_phase": _target_phase_for_latest_bridge(snapshot, bridge_window_id),
        "error_type": error.get("type"),
        "blocked_teammates": [str(item) for item in blocked_teammates if str(item)],
        "recommended_target_phase": _recommended_target_phase_from_reports(reports),
        "blocking_report_summaries": [
            {
                "classification": report.get("classification"),
                "summary": _bounded_text(report.get("summary"), 700),
                "next_action_recommendation": _bounded_text(report.get("next_action_recommendation"), 500),
            }
            for report in blocking_reports[:4]
        ],
    }


def _latest_reports(bridge_result: dict[str, Any]) -> list[dict[str, Any]]:
    raw = bridge_result.get("reports_preview")
    if not isinstance(raw, list):
        raw = bridge_result.get("reports")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _report_is_blocking(report: dict[str, Any]) -> bool:
    classification = str(report.get("classification") or "").strip()
    if classification in BLOCKING_REPORT_CLASSIFICATIONS:
        return True
    coverage = report.get("instruction_coverage")
    if isinstance(coverage, dict):
        return any(str(value).strip() in {"blocked", "hard_stop", "escalated"} for value in coverage.values())
    return False


def _target_phase_for_latest_bridge(snapshot: dict[str, Any], bridge_window_id: str) -> str | None:
    if not bridge_window_id:
        return None
    bindings = snapshot.get("bindings") if isinstance(snapshot.get("bindings"), dict) else {}
    windows = bindings.get("bridge_windows") if isinstance(bindings.get("bridge_windows"), dict) else {}
    binding = windows.get(bridge_window_id) if isinstance(windows.get(bridge_window_id), dict) else {}
    target = binding.get("target_phase")
    if target:
        return str(target)
    route = snapshot.get("route") if isinstance(snapshot.get("route"), dict) else {}
    target = route.get("target_phase")
    return str(target) if target else None


def _recommended_target_phase_from_reports(reports: list[dict[str, Any]]) -> str | None:
    for report in reports:
        text = str(report.get("next_action_recommendation") or "")
        target = _recommended_target_phase_from_text(text)
        if target:
            return target
    return None


def _recommended_target_phase_from_text(text: str) -> str | None:
    normalized = " ".join(str(text or "").split()).lower()
    if not normalized:
        return None
    if "l4_implement_then_l4_execute" in normalized:
        return "l4_implement"
    phase = r"(l2_advisory|l3_bridge|l4_implement|l4_execute|l4_anomaly)"
    patterns = [
        rf"\bnext legal route is\s+{phase}\b",
        rf"\broute next to(?: targeted)?\s+{phase}\b",
        rf"\bnext to(?: targeted)?\s+{phase}\b",
        rf"\broute to\s+{phase}\b",
        rf"\btarget_phase[\"'` ]*[:=]\s*[\"'` ]*{phase}\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            return match.group(1)
    return None


def _bounded_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


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
    phase_contracts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    teammates = _default_teammate_specs(target_phase, phase_contracts)
    ownership = _default_ownership_boundary(target_phase, phase_contracts)
    return {
        "team_id_or_null": None,
        "team_name": f"bridge-{target_phase}-team",
        "teammate_specs": teammates,
        "ownership_boundary": ownership,
    }


def _plan_team_for_task(
    target_phase: str,
    task_spec: dict[str, Any],
    team_spec: dict[str, Any],
    phase_contracts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    configured = phase_contracts.get("team_planner") if isinstance(phase_contracts, dict) and isinstance(phase_contracts.get("team_planner"), dict) else {}
    teammates = team_spec.get("teammate_specs") if isinstance(team_spec.get("teammate_specs"), list) else []
    decision = RiskBasedTeamSelector().select(
        target_phase=target_phase,
        task_spec=task_spec,
        policy_teammates=teammates,
        config=configured,
    )
    planned = deepcopy(team_spec)
    planned["teammate_specs"] = decision.selected_teammates
    decision_payload = decision.as_dict()
    decision_payload["policy_ref"] = "control/policy/phase_contracts.json#team_planner"
    decision_payload["original_teammate_names"] = [
        str(item.get("teammate_name") or "")
        for item in teammates
        if isinstance(item, dict) and item.get("teammate_name")
    ]
    planned["team_planning"] = decision_payload
    return {"team_spec": planned, "decision": decision_payload}


def _default_bridge_tools(target_phase: str, phase_contracts: dict[str, Any] | None = None) -> list[str]:
    config = _phase_config(phase_contracts, target_phase)
    configured = config.get("bridge_tools") if isinstance(config, dict) else None
    if isinstance(configured, list) and configured:
        return [str(item) for item in configured if str(item)]
    return list(PHASE_BRIDGE_TOOLS.get(target_phase, DEFAULT_BRIDGE_LEADER_TOOLS))


def _default_ownership_boundary(target_phase: str, phase_contracts: dict[str, Any] | None = None) -> dict[str, Any]:
    config = _phase_config(phase_contracts, target_phase)
    configured = config.get("ownership_boundary") if isinstance(config, dict) else None
    scopes = configured if isinstance(configured, dict) else PHASE_OWNERSHIP_DEFAULTS.get(target_phase, {"readable_scopes": ["."], "writable_scopes": []})
    forbidden = _contracts_list(phase_contracts, "default_forbidden_actions") or list(DEFAULT_FORBIDDEN_ACTIONS)
    return {
        "readable_scopes": list(scopes.get("readable_scopes", ["."])),
        "writable_scopes": list(scopes.get("writable_scopes", [])),
        "process_ownership_rules": ["only manage processes launched inside this bridge window"],
        "forbidden_actions": forbidden,
        "active_surface_policy": list(PHASE_ACTIVE_SURFACE_POLICIES.get(target_phase, [])),
    }


def _default_teammate_specs(target_phase: str, phase_contracts: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    config = _phase_config(phase_contracts, target_phase)
    configured = config.get("teammates") if isinstance(config, dict) else None
    if isinstance(configured, list) and configured:
        specs = []
        for teammate in configured:
            if not isinstance(teammate, dict):
                continue
            name = str(teammate.get("teammate_name") or "").strip()
            if not name:
                continue
            tools = teammate.get("allowed_tools") if isinstance(teammate.get("allowed_tools"), list) else []
            responsibilities = teammate.get("responsibilities") if isinstance(teammate.get("responsibilities"), list) else []
            specs.append(
                {
                    "teammate_id_or_null": teammate.get("teammate_id_or_null"),
                    "teammate_name": name,
                    "role": str(teammate.get("role") or "bridge teammate"),
                    "allowed_tools": [str(item) for item in tools if str(item)],
                    "responsibilities": [str(item) for item in responsibilities if str(item)],
                }
            )
        if specs:
            return specs
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
    phase_contracts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = deepcopy(task_spec or {})
    original_instruction = str(
        source.get("original_user_instruction")
        or source.get("user_instruction")
        or user_instruction
        or ""
    ).strip()
    subject = str(source.get("task_subject") or source.get("subject") or _derive_subject(original_instruction) or "bridge-window task")
    description = str(source.get("task_description") or source.get("description") or original_instruction or subject)
    normalized = {
        "task_id_or_null": source.get("task_id_or_null") or source.get("task_id"),
        "task_subject": subject,
        "task_description": description,
        "original_user_instruction": original_instruction,
        "instruction_coverage_checklist": _derive_instruction_coverage_checklist(source, original_instruction, description),
        "semantic_resolution_contract": _semantic_resolution_contract(source, original_instruction, target_phase, phase_contracts),
        "current_user_intent_context": _current_user_intent_context(source, original_instruction, description),
        "preserved_task_context": _preserved_task_context(source),
        "task_kind": str(source.get("task_kind") or "bridge_window_task"),
        "target_phase": target_phase,
        "completion_contract": deepcopy(completion_contract),
        "report_contract": deepcopy(report_contract),
    }
    return normalized


def _build_task_team_mapping(task_spec: dict[str, Any], team_spec: dict[str, Any], *, phase_contracts: dict[str, Any] | None = None) -> dict[str, Any]:
    assignments = []
    ownership = team_spec.get("ownership_boundary", {}) if isinstance(team_spec.get("ownership_boundary"), dict) else {}
    for teammate in team_spec.get("teammate_specs", []):
        name = str(teammate.get("teammate_name") or "bridge-worker")
        responsibilities = teammate.get("responsibilities") if isinstance(teammate.get("responsibilities"), list) else []
        assignment_body = "\n".join(
            [
                f"{name}: {task_spec['task_description']}",
                f"Original user instruction: {task_spec.get('original_user_instruction') or task_spec['task_description']}",
                f"Instruction coverage checklist: {_json_list(task_spec.get('instruction_coverage_checklist'))}",
                f"Semantic resolution contract: {_json_dict(task_spec.get('semantic_resolution_contract'))}",
                f"Current user intent context: {_json_dict(task_spec.get('current_user_intent_context'))}",
                f"Preserved task context: {_json_dict(task_spec.get('preserved_task_context'))}",
                "Coverage rule: do not mark the task complete until every checklist item is completed, explicitly deferred with a concrete reason, or escalated to main-leader/user.",
                "Semantic identity rule: resolve or explicitly carry model/method identity, checkpoint identity, dataset identity, prompt identity, code/config basis, and inherited defaults before downstream implementation or execution. Do not silently change them.",
                "Current intent rule: treat current_user_intent_context as the nearest active user intent for this bridge window. Confirm it, refine it, or supersede it from evidence; do not silently drop or rewrite it when recommending the next phase.",
                "Repairability rule: do not report blocked, escalated, or hard_stop for an issue that is inside the packet boundary, allowed tools, and writable scope and can be fixed by bounded debugging, dependency repair, cache repair, loader/export repair, script/config repair, retry, or resource-aware parameter adjustment. Treat repairable operational issues as current work and keep going.",
                "Escalation rule: ask main-leader/user or return hard_stop only when the next viable action needs a new semantic decision, broader scope, secret/token, paid access, manual click-through or license acceptance, destructive/global environment change, unavailable artifact, unresolved source identity, unsafe data exposure, or when bounded authorized repair attempts are exhausted with evidence.",
                "Report rule: include an instruction coverage section that lists completed, deferred, blocked, and escalated checklist items.",
                "Report rule: include a semantic identity resolution section with resolved, inherited, unknown, blocked, or escalated disposition for each required identity field.",
                "Report rule: include a current user intent context section that states confirmed, refined, superseded, blocked, or escalated disposition and the evidence for any change.",
                f"Role: {teammate.get('role') or 'bridge teammate'}",
                f"Responsibilities: {_json_list(responsibilities)}",
                f"Allowed tools: {_json_list(teammate.get('allowed_tools'))}",
                f"Readable scopes: {_json_list(ownership.get('readable_scopes'))}",
                f"Writable scopes: {_json_list(ownership.get('writable_scopes'))}",
                f"Forbidden actions: {_json_list(ownership.get('forbidden_actions'))}",
                f"Completion contract: {_json_dict(task_spec.get('completion_contract'))}",
                f"Report contract: {_json_dict(task_spec.get('report_contract'))}",
                *_phase_assignment_instructions(str(task_spec.get("target_phase") or ""), name, phase_contracts),
                "Do not read .claude/runtime_state/bridge_prompts for task context; that bridge prompt artifact is for audit only.",
                "When using Read, omit optional parameters you do not need. Never pass pages as an empty string.",
            ]
        )
        dispatch_description = f"{name}: {teammate.get('role') or 'bridge teammate'}"
        assignments.append(
            {
                "teammate_id_or_null": teammate.get("teammate_id_or_null"),
                "teammate_name": name,
                "assignment": assignment_body,
                "agent_dispatch": build_agent_dispatch(name, dispatch_description, assignment_body),
                "expected_output": "completion report and declared artifact refs",
            }
        )
    return {
        "task_id_or_null": task_spec.get("task_id_or_null"),
        "team_id_or_null": team_spec.get("team_id_or_null"),
        "teammate_assignments": assignments,
    }


def _phase_assignment_instructions(target_phase: str, teammate_name: str, phase_contracts: dict[str, Any] | None = None) -> list[str]:
    if target_phase != "l4_execute":
        return []
    manifest_fields = (
        phase_contracts.get("manifest_contracts", {}).get("formal_log_manifest_required_fields", [])
        if isinstance(phase_contracts, dict) and isinstance(phase_contracts.get("manifest_contracts"), dict)
        else []
    )
    if manifest_fields:
        return [f"Formal log manifest required fields: {_json_list(manifest_fields)}"]
    return []


def _phase_config(phase_contracts: dict[str, Any] | None, target_phase: str) -> dict[str, Any]:
    if not isinstance(phase_contracts, dict):
        return {}
    phases = phase_contracts.get("phases")
    if not isinstance(phases, dict):
        return {}
    config = phases.get(str(target_phase))
    return config if isinstance(config, dict) else {}


def _policy_contract_ref(phase_contracts: dict[str, Any]) -> dict[str, Any]:
    if not phase_contracts:
        return {"source": "main_leader_builtin_defaults", "schema_version": None}
    return {
        "source": "control/policy/phase_contracts.json",
        "schema_version": phase_contracts.get("schema_version"),
        "workflow_name": phase_contracts.get("workflow_name"),
    }


def _contracts_list(phase_contracts: dict[str, Any] | None, key: str) -> list[str]:
    if not isinstance(phase_contracts, dict):
        return []
    value = phase_contracts.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _extend_unique(target: dict[str, Any], key: str, values: Any) -> None:
    if not isinstance(values, list):
        return
    existing = [str(item) for item in target.get(key, []) if str(item)]
    target[key] = _dedupe_nonempty([*existing, *[str(item) for item in values if str(item)]])


def _json_list(value: Any) -> str:
    items = value if isinstance(value, list) else []
    return json.dumps([str(item) for item in items], ensure_ascii=False, default=str)


def _json_dict(value: Any) -> str:
    data = value if isinstance(value, dict) else {}
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def _default_completion_contract(target_phase: str | None = None, phase_contracts: dict[str, Any] | None = None) -> dict[str, Any]:
    contract = deepcopy(phase_contracts.get("base_completion_contract")) if isinstance(phase_contracts, dict) and isinstance(phase_contracts.get("base_completion_contract"), dict) else {}
    if not contract:
        contract = {
            "required_outputs": ["report"],
            "required_artifacts": [],
            "validation_requirements": [],
            "success_criteria": [
                "bridge leader collected a report from the team",
                "every instruction coverage checklist item is completed, deferred with reason, blocked, or escalated",
            ],
            "allowed_partial_result": True,
            "timeout_policy": deepcopy(DEFAULT_TIMEOUT_POLICY),
        }
    config = _phase_config(phase_contracts, str(target_phase or ""))
    timeout_policy = config.get("timeout_policy") if isinstance(config, dict) else None
    if isinstance(timeout_policy, dict):
        contract["timeout_policy"] = deepcopy(timeout_policy)
    elif str(target_phase or "") in PHASE_TIMEOUT_POLICY:
        contract["timeout_policy"] = deepcopy(PHASE_TIMEOUT_POLICY[str(target_phase or "")])
    else:
        contract.setdefault("timeout_policy", deepcopy(DEFAULT_TIMEOUT_POLICY))
    additions = config.get("completion_contract_additions") if isinstance(config, dict) else None
    if isinstance(additions, dict):
        _extend_unique(contract, "required_artifacts", additions.get("required_artifacts"))
        _extend_unique(contract, "validation_requirements", additions.get("validation_requirements"))
        _extend_unique(contract, "success_criteria", additions.get("success_criteria"))
    elif str(target_phase or "") == "l4_execute":
        contract["required_artifacts"] = ["log_manifest"]
        contract["validation_requirements"] = [
            "generated formal log folders include internal manifests",
            "log manifests include required identity command cwd batchbasis gpu memory and semantic fields",
        ]
        contract["success_criteria"].append("formal execution log folders are not identified by filename alone; each generated log folder has an internal manifest with identity, command, cwd, batchbasis, GPU/memory, and natural-language semantic fields")
    if str(target_phase or "") == "l4_execute" and isinstance(phase_contracts, dict):
        manifest_contracts = phase_contracts.get("manifest_contracts")
        if isinstance(manifest_contracts, dict) and isinstance(manifest_contracts.get("formal_log_manifest_required_fields"), list):
            contract["manifest_required_fields"] = [str(item) for item in manifest_contracts["formal_log_manifest_required_fields"] if str(item)]
    return contract


def _default_report_contract(target_phase: str | None = None, phase_contracts: dict[str, Any] | None = None) -> dict[str, Any]:
    contract = deepcopy(phase_contracts.get("base_report_contract")) if isinstance(phase_contracts, dict) and isinstance(phase_contracts.get("base_report_contract"), dict) else {}
    if not contract:
        contract = {
            "required_sections": ["summary", "evidence", "instruction_coverage", "semantic_identity_resolution"],
            "required_evidence": ["runtime event ids", "instruction coverage disposition", "semantic identity resolution"],
            "artifact_reporting_format": "list",
            "include_failure_reason": True,
            "include_next_action_recommendation": True,
        }
    taxonomy = phase_contracts.get("classification_taxonomy") if isinstance(phase_contracts, dict) else {}
    if isinstance(taxonomy, dict) and taxonomy:
        contract.setdefault("classification_taxonomy", deepcopy(taxonomy))
    config = _phase_config(phase_contracts, str(target_phase or ""))
    additions = config.get("report_contract_additions") if isinstance(config, dict) else None
    if isinstance(additions, dict):
        _extend_unique(contract, "required_sections", additions.get("required_sections"))
        _extend_unique(contract, "required_evidence", additions.get("required_evidence"))
        _extend_unique(contract, "hard_required_sections", additions.get("hard_required_sections"))
    else:
        if str(target_phase or "") == "l4_execute":
            contract["required_sections"].append("artifact_manifests")
            contract["required_evidence"].extend([
                "log manifest path",
                "formal execution parameter manifest",
                "manifest required fields checklist",
                "batchbasis",
                "gpu_id",
                "smoke memory observed when smoke ran",
                "warmup memory observed when warmup ran",
                "natural-language model dataset method semantics",
            ])
        if str(target_phase or "") == "l3_bridge":
            contract["required_sections"].append("current_user_intent_context")
            contract["required_evidence"].append("current user intent confirmed refined superseded blocked or escalated with reason")
        if str(target_phase or "") == "l2_advisory":
            contract["required_sections"].append("major_technical_plan_pseudocode")
            contract["required_evidence"].append("pseudocode flow for each new major technical plan or explicit not_applicable reason")
    contract["required_sections"] = _dedupe_nonempty([str(item) for item in contract.get("required_sections", [])])
    contract["required_evidence"] = _dedupe_nonempty([str(item) for item in contract.get("required_evidence", [])])
    contract["hard_required_sections"] = _dedupe_nonempty([str(item) for item in contract.get("hard_required_sections", [])])
    if str(target_phase or "") == "l4_execute" and isinstance(phase_contracts, dict):
        manifest_fields = []
        manifest_contracts = phase_contracts.get("manifest_contracts")
        if isinstance(manifest_contracts, dict) and isinstance(manifest_contracts.get("formal_log_manifest_required_fields"), list):
            manifest_fields = [str(item) for item in manifest_contracts["formal_log_manifest_required_fields"] if str(item)]
        if manifest_fields:
            contract.setdefault("manifest_required_fields", manifest_fields)
    return contract


def _derive_subject(original_instruction: str) -> str:
    text = " ".join(str(original_instruction or "").split())
    if not text:
        return ""
    return text[:72]


def _derive_instruction_coverage_checklist(source: dict[str, Any], original_instruction: str, description: str) -> list[str]:
    items: list[str] = []
    for key in (
        "instruction_coverage_checklist",
        "requirements",
        "acceptance_criteria",
        "constraints",
        "must_do",
        "must_not_do",
    ):
        items.extend(_string_items(source.get(key)))
    items.extend(_split_instruction_text(original_instruction))
    if not items:
        items.extend(_split_instruction_text(description))
    return _dedupe_nonempty(items) or [str(description)]


def _semantic_resolution_contract(source: dict[str, Any], original_instruction: str, target_phase: str, phase_contracts: dict[str, Any] | None = None) -> dict[str, Any]:
    supplied = source.get("semantic_resolution_contract")
    if isinstance(supplied, dict):
        contract = deepcopy(supplied)
    else:
        contract = {}
    configured = phase_contracts.get("semantic_resolution_contract") if isinstance(phase_contracts, dict) else {}
    required_fields = configured.get("required_identity_fields") if isinstance(configured, dict) and isinstance(configured.get("required_identity_fields"), list) else [
        "model_or_method_identity",
        "checkpoint_identity",
        "dataset_identity",
        "prompt_or_template_identity",
        "code_config_basis",
        "metric_or_objective_identity",
        "inherited_defaults",
    ]
    contract.setdefault("required_identity_fields", required_fields)
    contract.setdefault("user_instruction_preview", _derive_subject(original_instruction))
    contract.setdefault("target_phase", target_phase)
    contract.setdefault(
        "resolution_policy",
        configured.get("resolution_policy") if isinstance(configured, dict) and isinstance(configured.get("resolution_policy"), list) else [
            "actively resolve identities from the frozen instruction and current repository state",
            "if the user did not request a change, inherit the current active dataset/prompt/config basis and say where it came from",
            "for model or method comparisons, name the concrete checkpoints or checkpoint-selection rule for each side",
            "do not let L4 implement or execute infer unresolved identities silently",
            "unknown identity fields must be marked unknown, blocked, or escalated with a concrete reason",
        ],
    )
    taxonomy = phase_contracts.get("classification_taxonomy") if isinstance(phase_contracts, dict) else {}
    dispositions = taxonomy.get("semantic_disposition") if isinstance(taxonomy, dict) else None
    contract.setdefault(
        "report_disposition_values",
        dispositions if isinstance(dispositions, list) and dispositions else ["resolved", "inherited", "unknown", "blocked", "escalated", "not_applicable"],
    )
    return contract


def _current_user_intent_context(source: dict[str, Any], original_instruction: str, description: str) -> dict[str, Any]:
    supplied = (
        source.get("current_user_intent_context")
        or source.get("current_user_intent")
        or source.get("active_user_intent")
        or source.get("proposed_user_direction")
    )
    if isinstance(supplied, dict):
        context = deepcopy(supplied)
    else:
        context = {}
        if supplied:
            context["active_user_intent"] = deepcopy(supplied)
    context.setdefault("active_user_intent", original_instruction or description)
    context.setdefault("basis", "latest user instruction and task_spec fields")
    related_context = context.get("related_context") if isinstance(context.get("related_context"), dict) else {}
    related_context = deepcopy(related_context)
    for key in (
        "latest_user_request",
        "l2_advisory_summary",
        "l2_report_refs",
        "proposed_directions",
        "rejected_directions",
        "open_questions",
        "decision_basis",
        "prior_bridge_result_refs",
        "source_report_refs",
    ):
        if key in source and key not in related_context:
            related_context[key] = deepcopy(source[key])
    context["related_context"] = related_context
    context.setdefault(
        "disposition_policy",
        [
            "confirm when repo/docs/evidence support the active intent",
            "refine when the active intent is directionally right but needs narrower semantic or repo-facing basis",
            "supersede when evidence contradicts the active intent or a later user instruction changes it",
            "carry unresolved uncertainty forward as blocked or escalated instead of dropping it",
        ],
    )
    return context


def _preserved_task_context(source: dict[str, Any]) -> dict[str, Any]:
    reserved = {
        "task_id_or_null",
        "task_id",
        "task_subject",
        "subject",
        "task_description",
        "description",
        "original_user_instruction",
        "user_instruction",
        "instruction_coverage_checklist",
        "requirements",
        "acceptance_criteria",
        "constraints",
        "must_do",
        "must_not_do",
        "semantic_resolution_contract",
        "current_user_intent_context",
        "current_user_intent",
        "active_user_intent",
        "proposed_user_direction",
        "task_kind",
    }
    preserved = {}
    for key, value in source.items():
        if key not in reserved:
            preserved[str(key)] = deepcopy(value)
    return preserved


def _string_items(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    return _split_instruction_text(str(value))


def _split_instruction_text(text: str) -> list[str]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    pieces: list[str] = []
    for line in normalized.split("\n"):
        cleaned = line.strip()
        if not cleaned:
            continue
        cleaned = cleaned.lstrip("-*0123456789.、)） \t")
        for part in cleaned.replace("；", ";").replace("。", ";").split(";"):
            stripped = part.strip()
            if stripped:
                pieces.append(stripped)
    return pieces


def _legacy_split_instruction_text(text: str) -> list[str]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    pieces: list[str] = []
    for line in normalized.split("\n"):
        cleaned = line.strip()
        if not cleaned:
            continue
        cleaned = cleaned.lstrip("-*0123456789. \t")
        cleaned = cleaned.replace("\uff1b", ";").replace("\u3002", ".").replace("\uff0c", ",")
        for part in re.split(r";+|(?<=[.!?])\s+", cleaned):
            pieces.extend(_bounded_instruction_pieces(part))
    return pieces


def _bounded_instruction_pieces(text: str, *, limit: int = 220) -> list[str]:
    text = " ".join(str(text or "").split())
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    current = ""
    for part in re.split(r",\s+|\s+-\s+", text):
        if not part:
            continue
        candidate = f"{current}, {part}" if current else part
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            pieces.append(current)
        current = part
        while len(current) > limit:
            pieces.append(current[:limit].rstrip())
            current = current[limit:].lstrip()
    if current:
        pieces.append(current)
    return pieces


def _split_instruction_text(text: str) -> list[str]:
    return _legacy_split_instruction_text(text)


def _dedupe_nonempty(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item).strip()
        key = text.casefold()
        if text and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
