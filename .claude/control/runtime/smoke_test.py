from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import uuid

from bridge_sdk import call_bridge_sdk
from bridge.executors import BridgeExecutionRequest, CliBridgeExecutor, SdkBridgeExecutor, SimulateBridgeExecutor, bridge_executor_from_env
from claude_cli_executor import _allowed_tools
from claude_cli_executor import _bridge_leader_prompt
from claude_cli_executor import _ensure_project_agent_files
from claude_cli_executor import _parse_claude_payload
from claude_cli_executor import _parse_claude_stdout_envelope
from claude_cli_executor import _claude_print_stream_json_args
from claude_cli_executor import _redact_cmd
from claude_cli_executor import _required_agent_models
from claude_cli_executor import _run_claude_streaming
from claude_cli_executor import _sdk_stream_event_paths
from claude_cli_executor import _settings_args
from claude_cli_executor import simulated_team_executor
from artifact_refs import normalize_artifact_refs, validate_artifact_refs
from completion_validator import completion_succeeded, validate_bridge_completion
from main_leader import decide_next_bridge_packet
from outer_sdk import ClaudeAgentSdkOuterLeaderAdapter, OuterSdkHost, OuterSdkHostConfig, UnavailableOuterLeaderAdapter
from output_guardrails import validate_bridge_result, validate_completion_report, validate_log_manifest, validate_teammate_report
from policy_compiler import compile_policy
from repo_runtime import ensure_repo_registered, get_repo_runtime_root, list_registered_repos, resolve_repo_key
from retry_driver import dispatch_retry_action_stub, evaluate_retry_attempt, load_scheduled_retry_events
from retry_policy import decide_retry, load_retry_policies, packet_hash
from runtime_event_envelope import normalize_runtime_event, normalize_stream_record
from state_graph import load_state_graph, replay_run_state, validate_state_graph
from team_planner import RiskBasedTeamSelector
from workflow_runtime import dispatch_workflow_event
from workflow_runtime import reconcile_workflow_from_ledger


SEMANTIC_REQUIRED_FIELDS = [
    "model_or_method_identity",
    "checkpoint_identity",
    "dataset_identity",
    "prompt_or_template_identity",
    "code_config_basis",
    "metric_or_objective_identity",
    "inherited_defaults",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_fixture(root: Path) -> tuple[Path, Path]:
    control_root = root / "control"
    runs_root = control_root / "runtime_state" / "runs"
    run_root = runs_root / "run_demo"
    (control_root / "policy").mkdir(parents=True, exist_ok=True)
    run_root.mkdir(parents=True, exist_ok=True)

    phase_graph = {
        "phases": [
            {"name": "leader_freeze", "allowed_next_phases": ["l2_advisory", "l3_bridge"]},
            {"name": "l2_advisory", "allowed_next_phases": ["l3_bridge"]},
            {"name": "l3_bridge", "allowed_next_phases": ["l3_bridge", "leader_freeze", "l2_advisory", "l4_implement", "l4_execute", "l4_anomaly"]},
            {"name": "l4_implement", "allowed_next_phases": ["l4_execute", "l4_anomaly"]},
            {"name": "l4_execute", "allowed_next_phases": ["l4_anomaly", "l4_implement"]},
            {"name": "l4_anomaly", "allowed_next_phases": ["l3_bridge", "l4_implement", "l4_execute"]},
        ]
    }
    now = _now()
    run_ledger = {
        "schema_version": "0.4.0",
        "run_id": "run_demo",
        "main_session_id": "main_demo",
        "workflow_name": "bridge_window_workflow",
        "workflow_version": "0.4.0",
        "run_status": "in_progress",
        "current_phase": "l3_bridge",
        "semantic": {"frozen": {"goal": "smoke"}, "frozen_at": now, "requires_refresh": False},
        "scope": {"frozen": {"writable_scopes": ["tmp"]}, "frozen_at": now, "requires_refresh": False},
        "route": {
            "current_route": ["l3_bridge", "l4_execute"],
            "target_phase": "l4_execute",
            "is_stale": False,
            "decided_by_event_id": None,
        },
        "approval_state": {"pending": False, "active_approval_ids": [], "records": []},
        "hard_stop": {"active": False, "reason_code": None, "details": None, "task_id": None, "raised_at": None},
        "created_at": now,
        "updated_at": now,
        "closed_at": None,
    }
    _write_json(control_root / "policy" / "phase_graph.json", phase_graph)
    _write_json(control_root / "policy" / "approval_matrix.json", {"categories": {"control_default": {"allowed_event_kinds": ["retry_attempt_scheduled", "enter_anomaly"]}}})
    _write_json(control_root / "policy" / "reconcile_rules.json", {"schema_version": "0.4.0"})
    source_phase_contracts = Path(__file__).resolve().parents[1] / "policy" / "phase_contracts.json"
    if source_phase_contracts.exists():
        _write_json(control_root / "policy" / "phase_contracts.json", json.loads(source_phase_contracts.read_text(encoding="utf-8")))
    source_state_graph = Path(__file__).resolve().parents[1] / "policy" / "state_graph.json"
    if source_state_graph.exists():
        _write_json(control_root / "policy" / "state_graph.json", json.loads(source_state_graph.read_text(encoding="utf-8")))
    source_lifecycle = Path(__file__).resolve().parents[1] / "policy" / "lifecycle_transition_table.json"
    if source_lifecycle.exists():
        _write_json(control_root / "policy" / "lifecycle_transition_table.json", json.loads(source_lifecycle.read_text(encoding="utf-8")))
    source_schemas = Path(__file__).resolve().parents[1] / "schemas"
    if source_schemas.exists():
        for schema_path in source_schemas.glob("*.schema.json"):
            _write_json(control_root / "schemas" / schema_path.name, json.loads(schema_path.read_text(encoding="utf-8")))
    _write_json(run_root / "run_ledger.json", run_ledger)
    return control_root, runs_root


def packet(bridge_window_id: str, sub_session_id: str) -> dict:
    return {
        "schema_version": "0.1",
        "binding": {
            "run_id": "run_demo",
            "main_session_id": "main_demo",
            "sub_session_id": sub_session_id,
            "bridge_window_id": bridge_window_id,
            "parent_tool_use_id": f"tool_{bridge_window_id}",
        },
        "frozen_semantics": {"goal": "smoke"},
        "frozen_scope": {"writable_scopes": ["tmp"]},
        "phase_route": ["l3_bridge", "l4_execute"],
        "target_phase": "l4_execute",
        "team_spec": {
            "team_id_or_null": None,
            "team_name": f"team_{bridge_window_id}",
            "teammate_specs": [{"teammate_name": "worker", "role": "execute", "allowed_tools": ["Read"], "responsibilities": []}],
            "ownership_boundary": {"readable_scopes": [], "writable_scopes": ["tmp"], "process_ownership_rules": [], "forbidden_actions": []},
        },
        "task_spec": {
            "task_id_or_null": None,
            "task_subject": f"task_{bridge_window_id}",
            "task_description": "smoke task",
            "task_kind": "bridge_window_smoke",
            "target_phase": "l4_execute",
            "semantic_resolution_contract": {
                "required_identity_fields": SEMANTIC_REQUIRED_FIELDS,
                "resolution_policy": ["inherit current active basis unless the user explicitly changed it"],
                "report_disposition_values": ["resolved", "inherited", "unknown", "blocked", "escalated", "not_applicable"],
            },
            "completion_contract": {
                "required_outputs": ["report"],
                "required_artifacts": ["artifact", "log_manifest"],
                "validation_requirements": ["validated", "generated formal log folders include internal manifests", "log manifests include required identity command cwd batchbasis gpu memory and semantic fields"],
                "success_criteria": ["done"],
                "allowed_partial_result": False,
                "timeout_policy": {
                    "heartbeat_interval_seconds": 60,
                    "soft_timeout_seconds": 900,
                    "hard_timeout_seconds": 3600,
                    "timeout_action": "ask_main_leader",
                },
            },
            "report_contract": {
                "required_sections": ["summary", "evidence", "instruction_coverage", "semantic_identity_resolution", "artifact_manifests"],
                "required_evidence": ["runtime event ids", "artifact", "instruction coverage disposition", "semantic identity resolution", "log manifest path", "formal execution parameter manifest", "manifest required fields checklist", "batchbasis", "gpu_id", "smoke memory observed when smoke ran", "warmup memory observed when warmup ran", "natural-language model dataset method semantics"],
                "artifact_reporting_format": "list",
                "include_failure_reason": True,
                "include_next_action_recommendation": True,
            },
        },
        "task_team_mapping": {
            "task_id_or_null": None,
            "team_id_or_null": None,
            "teammate_assignments": [{"assignment": "execute", "expected_output": "report"}],
        },
        "completion_contract": {
            "required_outputs": ["report"],
            "required_artifacts": ["artifact", "log_manifest"],
            "validation_requirements": ["validated", "generated formal log folders include internal manifests", "log manifests include required identity command cwd batchbasis gpu memory and semantic fields"],
            "success_criteria": ["done"],
            "allowed_partial_result": False,
            "timeout_policy": {
                "heartbeat_interval_seconds": 60,
                "soft_timeout_seconds": 900,
                "hard_timeout_seconds": 3600,
                "timeout_action": "ask_main_leader",
            },
        },
        "report_contract": {
            "required_sections": ["summary", "evidence", "instruction_coverage", "semantic_identity_resolution", "artifact_manifests"],
            "required_evidence": ["runtime event ids", "artifact", "instruction coverage disposition", "semantic identity resolution", "log manifest path", "formal execution parameter manifest", "manifest required fields checklist", "batchbasis", "gpu_id", "smoke memory observed when smoke ran", "warmup memory observed when warmup ran", "natural-language model dataset method semantics"],
            "artifact_reporting_format": "list",
            "include_failure_reason": True,
            "include_next_action_recommendation": True,
        },
        "allowed_actions": ["team_create", "task_create", "send_messages", "task_complete", "team_delete"],
        "allowed_tools": ["Agent", "Read"],
        "approval_requirements": [],
        "created_at": _now(),
        "expires_at": None,
    }


def event(kind: str, bridge_window_id: str, sub_session_id: str, **kwargs: object) -> dict:
    payload = kwargs.pop("payload", {})
    return {
        "run_id": "run_demo",
        "main_session_id": "main_demo",
        "sub_session_id": sub_session_id,
        "bridge_window_id": bridge_window_id,
        "team_id": kwargs.pop("team_id", None),
        "task_id": kwargs.pop("task_id", None),
        "agent_id": kwargs.pop("agent_id", "bridge_leader_demo"),
        "agent_type": kwargs.pop("agent_type", "bridge-leader"),
        "tool_name": kwargs.pop("tool_name", None),
        "tool_use_id": kwargs.pop("tool_use_id", None),
        "event_kind": kind,
        "timestamp": kwargs.pop("timestamp", _now()),
        "payload": payload,
    }


def report(summary: str = "ok", *, item: str = "smoke checklist") -> dict:
    return {
        "summary": summary,
        "instruction_coverage": {item: "completed"},
        "semantic_identity_resolution": {"disposition": "not_applicable", "basis": "smoke fixture"},
        "evidence_refs": [f"event:{item.replace(' ', '_')}"],
        "manifest required fields checklist": _manifest_required_fields_checklist(),
    }


def coverage_for_packet(packet_payload: dict) -> dict:
    task = packet_payload.get("task_spec") if isinstance(packet_payload.get("task_spec"), dict) else {}
    checklist = task.get("instruction_coverage_checklist") if isinstance(task.get("instruction_coverage_checklist"), list) else []
    return {str(item): "completed" for item in checklist if str(item)} or {"smoke checklist": "completed"}


def report_for_packet(packet_payload: dict, summary: str = "ok") -> dict:
    return {
        "summary": summary,
        "instruction_coverage": coverage_for_packet(packet_payload),
        "semantic_identity_resolution": {"disposition": "not_applicable", "basis": "smoke fixture"},
        "evidence_refs": [f"event:{packet_payload['binding']['bridge_window_id']}"],
        "manifest required fields checklist": _manifest_required_fields_checklist(packet_payload),
    }


def _manifest_required_fields_checklist(packet_payload: dict | None = None) -> dict:
    bridge_window_id = packet_payload.get("binding", {}).get("bridge_window_id") if isinstance(packet_payload, dict) else "present"
    return {
        "run_id": "run_demo",
        "bridge_window_id": bridge_window_id or "present",
        "task_id": "task_demo",
        "command": "conda run -n mjy python smoke.py",
        "cwd": ".",
        "batchbasis": "smoke-derived batch basis",
        "gpu_id": "0",
        "gpu_id_or_device_ids": "0",
        "memory observed": "smoke memory observed; warmup memory observed; formal memory observed",
        "formal_memory_observed": "72GB",
        "model": "demo model",
        "model_or_model_family": "demo model",
        "dataset": "demo dataset",
        "dataset_name_split_source": "demo dataset",
        "method": "demo method",
        "method_or_objective": "demo method",
        "terminal_status": "succeeded",
    }


def dispatch(control_root: Path, runs_root: Path, payload: dict) -> dict:
    result = dispatch_workflow_event(str(control_root), payload, runtime_runs_root=str(runs_root), persist=True)
    if not result.ok:
        raise AssertionError(json.dumps(result.check_result, ensure_ascii=False))
    return result.runtime_snapshot


def run_success(control_root: Path, runs_root: Path) -> dict:
    bw = "bw_success"
    ss = "sub_success"
    p = packet(bw, ss)
    dispatch(control_root, runs_root, event("bridge_call_intended", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_success", payload={"packet": p}))
    dispatch(control_root, runs_root, event("pretooluse_allowed_by_main_leader", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_success", payload={"packet": p}))
    dispatch(control_root, runs_root, event("call_bridge_sdk_started", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_success", payload={"packet": p}))
    dispatch(control_root, runs_root, event("bridge_window_opened", bw, ss, agent_type="bridge-leader", payload={"packet": p}))
    dispatch(control_root, runs_root, event("bridge_packet_accepted", bw, ss, payload={"packet": p}))
    dispatch(control_root, runs_root, event("team_create_started", bw, ss, team_id="team_success", tool_name="team_create"))
    dispatch(control_root, runs_root, event("team_create_succeeded", bw, ss, team_id="team_success", tool_name="team_create", payload={"team_name": "team_success", "teammate_ids": ["mate_1"]}))
    dispatch(control_root, runs_root, event("task_create_started", bw, ss, team_id="team_success", task_id="task_success", tool_name="task_create"))
    dispatch(control_root, runs_root, event("task_create_succeeded", bw, ss, team_id="team_success", task_id="task_success", tool_name="task_create"))
    dispatch(
        control_root,
        runs_root,
        event(
            "taskcreated_hook_accepted",
            bw,
            ss,
            team_id="team_success",
            task_id="task_success",
            agent_type="hook",
            agent_id="hook.task_created",
            payload={
                "task_subject": "task_bw_success",
                "task_description": "smoke task created",
                "task_spec": p["task_spec"],
                "team_spec": p["team_spec"],
                "task_team_mapping": p["task_team_mapping"],
                "teammate_ids": ["mate_1"],
            },
        ),
    )
    dispatch(control_root, runs_root, event("message_dispatch_started", bw, ss, team_id="team_success", task_id="task_success", tool_name="send_messages"))
    dispatch(control_root, runs_root, event("message_dispatch_succeeded", bw, ss, team_id="team_success", task_id="task_success", tool_name="send_messages"))
    dispatch(control_root, runs_root, event("team_idle_waiting", bw, ss, team_id="team_success", task_id="task_success", agent_type="hook", agent_id="hook.team_idle", payload={"wait_reason": "process_running", "owned_process_refs": []}))
    dispatch(control_root, runs_root, event("artifacts_ready", bw, ss, team_id="team_success", task_id="task_success", tool_name="task_complete"))
    dispatch(control_root, runs_root, event("completion_contract_satisfied", bw, ss, team_id="team_success", task_id="task_success", agent_type="hook", agent_id="hook.task_completed", payload={"completion_contract": p["completion_contract"], "completion_checks": {"required_outputs_present": True, "required_artifacts_present": True, "validation_passed": True, "missing_outputs": [], "missing_artifacts": [], "failed_validations": [], "notes": []}, "completion_evidence": {"trajectory_refs": ["trajectory.jsonl:1"], "manifest_required_fields_checklist": _manifest_required_fields_checklist(p)}, "reports": [report_for_packet(p)], "artifact_refs": ["artifact", "log_manifest"]}))
    dispatch(control_root, runs_root, event("team_delete_started", bw, ss, team_id="team_success", task_id="task_success", tool_name="team_delete"))
    dispatch(control_root, runs_root, event("team_delete_succeeded", bw, ss, team_id="team_success", task_id="task_success", tool_name="team_delete"))
    return dispatch(control_root, runs_root, event("bridge_result_returned", bw, ss, team_id="team_success", task_id="task_success", agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", payload={"bridge_result": {"status": "succeeded", "reports": [report_for_packet(p)], "artifact_refs": ["artifact", "log_manifest"], "evidence": {"event_ids": ["evt_success"], "trajectory_refs": ["trajectory.jsonl:1"], "manifest_required_fields_checklist": _manifest_required_fields_checklist(p)}, "error_or_null": None, "cleanup_required": False}}))


def run_failure(control_root: Path, runs_root: Path) -> dict:
    bw = "bw_failed"
    ss = "sub_failed"
    p = packet(bw, ss)
    dispatch(control_root, runs_root, event("bridge_call_intended", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_failed", payload={"packet": p}))
    dispatch(control_root, runs_root, event("pretooluse_allowed_by_main_leader", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_failed", payload={"packet": p}))
    return dispatch(control_root, runs_root, event("call_bridge_sdk_error", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_failed", payload={"error_or_null": {"message": "sdk failed"}}))


def run_orphan(control_root: Path, runs_root: Path) -> dict:
    bw = "bw_orphan"
    ss = "sub_orphan"
    p = packet(bw, ss)
    dispatch(control_root, runs_root, event("bridge_call_intended", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_orphan", payload={"packet": p}))
    dispatch(control_root, runs_root, event("pretooluse_allowed_by_main_leader", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_orphan", payload={"packet": p}))
    dispatch(control_root, runs_root, event("call_bridge_sdk_started", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_orphan", payload={"packet": p}))
    return dispatch(control_root, runs_root, event("orphan_timeout_without_bridge_return", bw, ss, agent_type="runtime", agent_id="orphan_scanner", payload={"last_known_event_ref": "call_bridge_sdk_started"}))


def run_manual_interrupt_recovery(control_root: Path, runs_root: Path) -> dict:
    bw = "bw_manual_interrupt"
    ss = "sub_manual_interrupt"
    p = packet(bw, ss)
    dispatch(control_root, runs_root, event("bridge_call_intended", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_manual_interrupt", payload={"packet": p}))
    dispatch(control_root, runs_root, event("pretooluse_allowed_by_main_leader", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_manual_interrupt", payload={"packet": p}))
    dispatch(control_root, runs_root, event("call_bridge_sdk_started", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_manual_interrupt", payload={"packet": p}))
    dispatch(control_root, runs_root, event("bridge_window_opened", bw, ss, agent_type="bridge-leader", payload={"packet": p}))
    dispatch(control_root, runs_root, event("bridge_packet_accepted", bw, ss, payload={"packet": p}))
    dispatch(control_root, runs_root, event("team_create_started", bw, ss, team_id="team_manual_interrupt", tool_name="team_create"))
    dispatch(control_root, runs_root, event("team_create_succeeded", bw, ss, team_id="team_manual_interrupt", tool_name="team_create", payload={"team_name": "team_manual_interrupt", "teammate_ids": ["mate_1"]}))
    dispatch(control_root, runs_root, event("task_create_started", bw, ss, team_id="team_manual_interrupt", task_id="task_manual_interrupt", tool_name="task_create"))
    dispatch(control_root, runs_root, event("task_create_succeeded", bw, ss, team_id="team_manual_interrupt", task_id="task_manual_interrupt", tool_name="task_create"))
    dispatch(
        control_root,
        runs_root,
        event(
            "taskcreated_hook_accepted",
            bw,
            ss,
            team_id="team_manual_interrupt",
            task_id="task_manual_interrupt",
            agent_type="hook",
            agent_id="hook.task_created",
            payload={
                "task_subject": "task_bw_manual_interrupt",
                "task_description": "smoke task created",
                "task_spec": p["task_spec"],
                "team_spec": p["team_spec"],
                "task_team_mapping": p["task_team_mapping"],
                "teammate_ids": ["mate_1"],
            },
        ),
    )
    dispatch(control_root, runs_root, event("message_dispatch_started", bw, ss, team_id="team_manual_interrupt", task_id="task_manual_interrupt", tool_name="send_messages"))
    dispatch(control_root, runs_root, event("message_dispatch_succeeded", bw, ss, team_id="team_manual_interrupt", task_id="task_manual_interrupt", tool_name="send_messages"))
    interrupted = dispatch(
        control_root,
        runs_root,
        event(
            "bridge_call_interrupted",
            bw,
            ss,
            team_id="team_manual_interrupt",
            task_id="task_manual_interrupt",
            agent_type="runtime",
            agent_id="runtime.interrupt",
            tool_name="call_bridge_sdk",
            payload={"interrupt_source": "manual_user_interrupt"},
        ),
    )
    if interrupted["lifecycle"]["status_index"].get(bw) != "bridge_window_interrupted":
        raise AssertionError(json.dumps(interrupted["lifecycle"], ensure_ascii=False, indent=2))
    if bw in interrupted["lifecycle"].get("open_bridge_window_ids", []):
        raise AssertionError(json.dumps(interrupted["lifecycle"], ensure_ascii=False, indent=2))
    if "call_bridge_sdk" not in interrupted.get("allowed_actions", []):
        raise AssertionError(json.dumps(interrupted, ensure_ascii=False, indent=2))
    decide_next_bridge_packet(
        str(control_root),
        "run_demo",
        runtime_runs_root=str(runs_root),
        main_session_id="main_demo",
        user_instruction="next bridge after manual interrupt",
        target_phase="l4_execute",
    )
    return interrupted


def run_stuck_dispatch_anomaly(control_root: Path, runs_root: Path) -> dict:
    bw = "bw_stuck_dispatch"
    ss = "sub_stuck_dispatch"
    p = packet(bw, ss)
    old_timestamp = "2020-01-01T00:00:00+00:00"
    dispatch(control_root, runs_root, event("bridge_call_intended", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_stuck_dispatch", payload={"packet": p}, timestamp=old_timestamp))
    dispatch(control_root, runs_root, event("pretooluse_allowed_by_main_leader", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_stuck_dispatch", payload={"packet": p}, timestamp=old_timestamp))
    dispatch(control_root, runs_root, event("call_bridge_sdk_started", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_stuck_dispatch", payload={"packet": p}, timestamp=old_timestamp))
    dispatch(control_root, runs_root, event("bridge_window_opened", bw, ss, agent_type="bridge-leader", payload={"packet": p}, timestamp=old_timestamp))
    dispatch(control_root, runs_root, event("bridge_packet_accepted", bw, ss, payload={"packet": p}, timestamp=old_timestamp))
    dispatch(control_root, runs_root, event("team_create_started", bw, ss, team_id="team_stuck_dispatch", tool_name="team_create", timestamp=old_timestamp))
    dispatch(control_root, runs_root, event("team_create_succeeded", bw, ss, team_id="team_stuck_dispatch", tool_name="team_create", payload={"team_name": "team_stuck_dispatch", "teammate_ids": ["mate_1"]}, timestamp=old_timestamp))
    dispatch(control_root, runs_root, event("task_create_started", bw, ss, team_id="team_stuck_dispatch", task_id="task_stuck_dispatch", tool_name="task_create", timestamp=old_timestamp))
    dispatch(control_root, runs_root, event("task_create_succeeded", bw, ss, team_id="team_stuck_dispatch", task_id="task_stuck_dispatch", tool_name="task_create", timestamp=old_timestamp))
    dispatch(
        control_root,
        runs_root,
        event(
            "taskcreated_hook_accepted",
            bw,
            ss,
            team_id="team_stuck_dispatch",
            task_id="task_stuck_dispatch",
            agent_type="hook",
            agent_id="hook.task_created",
            payload={
                "task_subject": "task_bw_stuck_dispatch",
                "task_description": "smoke task created",
                "task_spec": p["task_spec"],
                "team_spec": p["team_spec"],
                "task_team_mapping": p["task_team_mapping"],
                "teammate_ids": ["mate_1"],
            },
            timestamp=old_timestamp,
        ),
    )
    dispatch(control_root, runs_root, event("message_dispatch_started", bw, ss, team_id="team_stuck_dispatch", task_id="task_stuck_dispatch", tool_name="send_messages", timestamp=old_timestamp))
    snapshot = dispatch(control_root, runs_root, event("message_dispatch_succeeded", bw, ss, team_id="team_stuck_dispatch", task_id="task_stuck_dispatch", tool_name="send_messages", timestamp=old_timestamp))
    diagnostics = snapshot.get("runtime_diagnostics", {})
    anomalies = diagnostics.get("orchestration_anomalies", [])
    if not anomalies:
        raise AssertionError(json.dumps(snapshot, ensure_ascii=False, indent=2))
    anomaly = anomalies[0]
    required_conditions = {"bridge_window_open_too_long", "status_stuck_at_message_dispatch_completed", "no_process_refs", "no_reports", "no_artifacts"}
    if anomaly.get("classification") != "bridge_orchestration_hang" or not required_conditions.issubset(set(anomaly.get("conditions", []))):
        raise AssertionError(json.dumps(anomaly, ensure_ascii=False, indent=2))
    if not snapshot.get("integrity", {}).get("has_blocking_orchestration_anomaly"):
        raise AssertionError(json.dumps(snapshot.get("integrity", {}), ensure_ascii=False, indent=2))
    return snapshot


def run_execute_watchdog_alert(control_root: Path, runs_root: Path) -> dict:
    bw = "bw_execute_watchdog"
    ss = "sub_execute_watchdog"
    p = packet(bw, ss)
    old_timestamp = "2020-01-01T00:00:00+00:00"
    dispatch(control_root, runs_root, event("bridge_call_intended", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_execute_watchdog", payload={"packet": p}, timestamp=old_timestamp))
    dispatch(control_root, runs_root, event("pretooluse_allowed_by_main_leader", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_execute_watchdog", payload={"packet": p}, timestamp=old_timestamp))
    dispatch(control_root, runs_root, event("call_bridge_sdk_started", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_execute_watchdog", payload={"packet": p}, timestamp=old_timestamp))
    dispatch(control_root, runs_root, event("bridge_window_opened", bw, ss, agent_type="bridge-leader", payload={"packet": p}, timestamp=old_timestamp))
    dispatch(control_root, runs_root, event("bridge_packet_accepted", bw, ss, payload={"packet": p}, timestamp=old_timestamp))
    dispatch(control_root, runs_root, event("team_create_started", bw, ss, team_id="team_execute_watchdog", tool_name="team_create", timestamp=old_timestamp))
    dispatch(control_root, runs_root, event("team_create_succeeded", bw, ss, team_id="team_execute_watchdog", tool_name="team_create", payload={"team_name": "team_execute_watchdog", "teammate_ids": ["mate_1"]}, timestamp=old_timestamp))
    dispatch(control_root, runs_root, event("task_create_started", bw, ss, team_id="team_execute_watchdog", task_id="task_execute_watchdog", tool_name="task_create", timestamp=old_timestamp))
    dispatch(control_root, runs_root, event("task_create_succeeded", bw, ss, team_id="team_execute_watchdog", task_id="task_execute_watchdog", tool_name="task_create", timestamp=old_timestamp))
    dispatch(
        control_root,
        runs_root,
        event(
            "taskcreated_hook_accepted",
            bw,
            ss,
            team_id="team_execute_watchdog",
            task_id="task_execute_watchdog",
            agent_type="hook",
            agent_id="hook.task_created",
            payload={
                "task_subject": "task_bw_execute_watchdog",
                "task_description": "smoke task created",
                "task_spec": p["task_spec"],
                "team_spec": p["team_spec"],
                "task_team_mapping": p["task_team_mapping"],
                "teammate_ids": ["mate_1"],
            },
            timestamp=old_timestamp,
        ),
    )
    dispatch(control_root, runs_root, event("message_dispatch_started", bw, ss, team_id="team_execute_watchdog", task_id="task_execute_watchdog", tool_name="send_messages", timestamp=old_timestamp))
    dispatch(control_root, runs_root, event("message_dispatch_succeeded", bw, ss, team_id="team_execute_watchdog", task_id="task_execute_watchdog", tool_name="send_messages", timestamp=old_timestamp))
    snapshot = dispatch(
        control_root,
        runs_root,
        event(
            "team_idle_waiting",
            bw,
            ss,
            team_id="team_execute_watchdog",
            task_id="task_execute_watchdog",
            agent_type="hook",
            agent_id="hook.team_idle",
            payload={
                "wait_reason": "process_running",
                "owned_process_refs": [{"process_ref": "proc_demo", "pid": 1234, "status": "running", "log_path": "logs/train.log"}],
                "last_heartbeat_at": old_timestamp,
                "timeout_policy": {"heartbeat_interval_seconds": 60, "soft_timeout_seconds": 21600, "hard_timeout_seconds": 86400},
            },
            timestamp=old_timestamp,
        ),
    )
    alerts = snapshot.get("runtime_diagnostics", {}).get("execute_watchdog_alerts", [])
    if not alerts:
        raise AssertionError(json.dumps(snapshot, ensure_ascii=False, indent=2))
    if alerts[0].get("classification") != "execute_stale_heartbeat_with_owned_process_refs":
        raise AssertionError(json.dumps(alerts[0], ensure_ascii=False, indent=2))
    if not snapshot.get("integrity", {}).get("has_execute_watchdog_alert"):
        raise AssertionError(json.dumps(snapshot.get("integrity", {}), ensure_ascii=False, indent=2))
    return snapshot


def run_user_clarification_resume(control_root: Path, runs_root: Path) -> dict:
    bw = "bw_user_clarification"
    ss = "sub_user_clarification"
    p = packet(bw, ss)
    p["target_phase"] = "l3_bridge"
    p["phase_route"] = ["l3_bridge"]
    p["task_spec"]["target_phase"] = "l3_bridge"
    p["team_spec"]["ownership_boundary"]["writable_scopes"] = ["."]
    dispatch(control_root, runs_root, event("bridge_call_intended", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_user_clarification", payload={"packet": p}))
    dispatch(control_root, runs_root, event("pretooluse_allowed_by_main_leader", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_user_clarification", payload={"packet": p}))
    dispatch(control_root, runs_root, event("call_bridge_sdk_started", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_user_clarification", payload={"packet": p}))
    dispatch(control_root, runs_root, event("bridge_window_opened", bw, ss, agent_type="bridge-leader", payload={"packet": p}))
    dispatch(control_root, runs_root, event("bridge_packet_accepted", bw, ss, payload={"packet": p}))
    dispatch(control_root, runs_root, event("team_create_started", bw, ss, team_id="team_user_clarification", tool_name="team_create"))
    dispatch(control_root, runs_root, event("team_create_succeeded", bw, ss, team_id="team_user_clarification", tool_name="team_create", payload={"team_name": "team_user_clarification", "teammate_ids": ["mate_1"]}))
    dispatch(control_root, runs_root, event("task_create_started", bw, ss, team_id="team_user_clarification", task_id="task_user_clarification", tool_name="task_create"))
    dispatch(control_root, runs_root, event("task_create_succeeded", bw, ss, team_id="team_user_clarification", task_id="task_user_clarification", tool_name="task_create"))
    dispatch(
        control_root,
        runs_root,
        event(
            "taskcreated_hook_accepted",
            bw,
            ss,
            team_id="team_user_clarification",
            task_id="task_user_clarification",
            agent_type="hook",
            agent_id="hook.task_created",
            payload={
                "task_subject": "task_bw_user_clarification",
                "task_description": "smoke task created",
                "task_spec": p["task_spec"],
                "team_spec": p["team_spec"],
                "task_team_mapping": p["task_team_mapping"],
                "teammate_ids": ["mate_1"],
            },
        ),
    )
    dispatch(control_root, runs_root, event("message_dispatch_started", bw, ss, team_id="team_user_clarification", task_id="task_user_clarification", tool_name="send_messages"))
    dispatch(control_root, runs_root, event("message_dispatch_succeeded", bw, ss, team_id="team_user_clarification", task_id="task_user_clarification", tool_name="send_messages"))
    dispatch(control_root, runs_root, event("user_clarification_required", bw, ss, team_id="team_user_clarification", task_id="task_user_clarification", payload={"question": "Confirm docs wording before refresh"}))
    paused = dispatch(
        control_root,
        runs_root,
        event(
            "bridge_result_returned_with_user_clarification_request",
            bw,
            ss,
            team_id="team_user_clarification",
            task_id="task_user_clarification",
            agent_type="main-leader",
            agent_id="main",
            tool_name="call_bridge_sdk",
            payload={"bridge_result": {"status": "needs_user_answer", "reports": [{"summary": "needs clarification"}], "artifact_refs": [], "evidence": {"question": "Confirm docs wording before refresh"}}},
        ),
    )
    if "call_bridge_sdk" in paused.get("allowed_actions", []):
        raise AssertionError(json.dumps(paused, ensure_ascii=False, indent=2))
    dispatch(control_root, runs_root, event("user_answer_received", bw, ss, agent_type="main-leader", agent_id="main", payload={"answer": "Proceed with the documented wording"}))
    dispatch(control_root, runs_root, event("resume_same_l3_task", bw, ss, agent_type="main-leader", agent_id="main", payload={"resume_reason": "user answered clarification"}))
    resumed = dispatch(control_root, runs_root, event("continuation_of_previous_l3", bw, ss, agent_type="main-leader", agent_id="main", payload={"continuation_reason": "continue bounded L3 documentation refresh"}))
    return resumed


def run_mcp_lifecycle_helper(control_root: Path, runs_root: Path) -> dict:
    mcp_path = Path(__file__).resolve().parents[1] / "mcp" / "bridge_server.py"
    spec = importlib.util.spec_from_file_location("bridge_server_smoke", mcp_path)
    if spec is None or spec.loader is None:
        raise AssertionError(str(mcp_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    bw = "bw_mcp_helper"
    ss = "sub_mcp_helper"
    p = packet(bw, ss)
    module._ensure_main_bridge_lifecycle_started(str(control_root), p, str(runs_root), persist=True)
    return dispatch(control_root, runs_root, event("orphan_timeout_without_bridge_return", bw, ss, agent_type="runtime", agent_id="orphan_scanner", payload={"last_known_event_ref": "call_bridge_sdk_started"}))


def run_sdk_roundtrip(control_root: Path, runs_root: Path) -> dict:
    packet = decide_next_bridge_packet(
        str(control_root),
        "run_demo",
        runtime_runs_root=str(runs_root),
        main_session_id="main_demo",
        user_instruction="run sdk bridge smoke task",
        task_spec={"task_subject": "sdk bridge smoke", "task_kind": "bridge_window_smoke"},
    )
    bridge_result = call_bridge_sdk(str(control_root), packet, runtime_runs_root=str(runs_root), persist=True, team_executor=simulated_team_executor)
    if bridge_result.get("status") != "succeeded":
        raise AssertionError(json.dumps(bridge_result, ensure_ascii=False, indent=2))
    replay = reconcile_workflow_from_ledger(str(control_root), "run_demo", runtime_runs_root=str(runs_root), persist=True)
    status = replay["runtime_snapshot"]["lifecycle"]["status_index"][packet["binding"]["bridge_window_id"]]
    if status != "bridge_window_returned":
        raise AssertionError(json.dumps(replay, ensure_ascii=False, indent=2))
    run_root = runs_root / "run_demo"
    companion_packets = _read_jsonl(run_root / "bridge_packets.jsonl")
    companion_messages = _read_jsonl(run_root / "agent_messages.jsonl")
    companion_tools = _read_jsonl(run_root / "tool_events.jsonl")
    companion_reports = _read_jsonl(run_root / "teammate_reports.jsonl")
    companion_artifacts = _read_jsonl(run_root / "artifacts.jsonl")
    companion_checks = _read_jsonl(run_root / "completion_checks.jsonl")
    companion_all = _read_jsonl(run_root / "companion_events.jsonl")
    if not companion_packets or not companion_messages or not companion_tools or not companion_reports or not companion_checks or not companion_all:
        raise AssertionError(
            json.dumps(
                {
                    "bridge_packets": companion_packets,
                    "agent_messages": companion_messages,
                    "tool_events": companion_tools,
                    "teammate_reports": companion_reports,
                    "artifacts": companion_artifacts,
                    "completion_checks": companion_checks,
                    "companion_events": companion_all,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    if not any(item.get("instruction_coverage_checklist") for item in companion_packets):
        raise AssertionError(json.dumps(companion_packets, ensure_ascii=False, indent=2))
    if not all(item.get("sequence") and item.get("monotonic_index") for item in companion_tools[:3]):
        raise AssertionError(json.dumps(companion_tools[:3], ensure_ascii=False, indent=2))
    if not any("safe_input_preview" in item and "file_refs" in item and "output_summary" in item for item in companion_tools):
        raise AssertionError(json.dumps(companion_tools, ensure_ascii=False, indent=2))
    if not any(item.get("message_id") and item.get("direction") == "bridge_leader_to_teammate" and "coverage_refs" in item for item in companion_messages):
        raise AssertionError(json.dumps(companion_messages, ensure_ascii=False, indent=2))
    if not any(item.get("progress_state") and "completed_items" in item and "file_refs" in item for item in companion_reports):
        raise AssertionError(json.dumps(companion_reports, ensure_ascii=False, indent=2))
    if not any(item.get("check_type") == "completion_contract" and isinstance(item.get("items"), list) for item in companion_checks):
        raise AssertionError(json.dumps(companion_checks, ensure_ascii=False, indent=2))
    if not any(item.get("source_kind") and item.get("source_file") and item.get("source_sequence") for item in companion_all):
        raise AssertionError(json.dumps(companion_all[:5], ensure_ascii=False, indent=2))
    event_log = _read_jsonl(run_root / "event_log.jsonl")
    transitions = _read_jsonl(run_root / "transitions.jsonl")
    if not any((item.get("runtime_event") or {}).get("authority") == "authoritative" for item in event_log):
        raise AssertionError(json.dumps(event_log[:5], ensure_ascii=False, indent=2))
    if not any((item.get("runtime_event") or {}).get("authority") == "authoritative" for item in transitions):
        raise AssertionError(json.dumps(transitions[:5], ensure_ascii=False, indent=2))
    if not any((item.get("runtime_event") or {}).get("authority") == "projection" for item in companion_all):
        raise AssertionError(json.dumps(companion_all[:5], ensure_ascii=False, indent=2))
    failed_packet = decide_next_bridge_packet(
        str(control_root),
        "run_demo",
        runtime_runs_root=str(runs_root),
        main_session_id="main_demo",
        user_instruction="run failed sdk bridge smoke task",
        task_spec={"task_subject": "sdk bridge failure smoke", "task_kind": "bridge_window_smoke"},
    )
    failed_result = call_bridge_sdk(
        str(control_root),
        failed_packet,
        runtime_runs_root=str(runs_root),
        persist=True,
        team_executor=lambda _: {"status": "failed", "reports": [{"summary": "failed"}], "error_or_null": {"message": "intentional failure"}},
    )
    if failed_result.get("status") != "failed":
        raise AssertionError(json.dumps(failed_result, ensure_ascii=False, indent=2))
    failed_events = [
        item
        for item in _read_jsonl(runs_root / "run_demo" / "event_log.jsonl")
        if item.get("bridge_window_id") == failed_packet["binding"]["bridge_window_id"]
    ]
    failed_event_kinds = [item.get("event_kind") for item in failed_events]
    if "team_executor_failed" not in failed_event_kinds:
        raise AssertionError(json.dumps(failed_event_kinds, ensure_ascii=False, indent=2))
    if "wait_timeout_or_process_lost" in failed_event_kinds:
        raise AssertionError(json.dumps(failed_event_kinds, ensure_ascii=False, indent=2))

    exception_packet = decide_next_bridge_packet(
        str(control_root),
        "run_demo",
        runtime_runs_root=str(runs_root),
        main_session_id="main_demo",
        user_instruction="run executor exception sdk bridge smoke task",
        task_spec={"task_subject": "sdk bridge exception smoke", "task_kind": "bridge_window_smoke"},
    )
    exception_result = call_bridge_sdk(
        str(control_root),
        exception_packet,
        runtime_runs_root=str(runs_root),
        persist=True,
        team_executor=lambda _: (_ for _ in ()).throw(FileNotFoundError("[WinError 206] file name too long")),
    )
    if exception_result.get("status") != "failed" or exception_result.get("failure_stage_or_null") != "team_wait":
        raise AssertionError(json.dumps(exception_result, ensure_ascii=False, indent=2))
    exception_events = [
        item
        for item in _read_jsonl(runs_root / "run_demo" / "event_log.jsonl")
        if item.get("bridge_window_id") == exception_packet["binding"]["bridge_window_id"]
    ]
    exception_event_kinds = [item.get("event_kind") for item in exception_events]
    if "team_executor_failed" not in exception_event_kinds or "wait_timeout_or_process_lost" in exception_event_kinds:
        raise AssertionError(json.dumps(exception_event_kinds, ensure_ascii=False, indent=2))

    partial_packet = decide_next_bridge_packet(
        str(control_root),
        "run_demo",
        runtime_runs_root=str(runs_root),
        main_session_id="main_demo",
        user_instruction="run partial sdk bridge smoke task",
        task_spec={"task_subject": "sdk bridge partial smoke", "task_kind": "bridge_window_smoke"},
    )
    partial_result = call_bridge_sdk(
        str(control_root),
        partial_packet,
        runtime_runs_root=str(runs_root),
        persist=True,
        team_executor=lambda _: {"status": "partial", "reports": [report("partial")], "evidence": {"reason": "intentional partial"}},
    )
    if partial_result.get("status") != "partial_or_failed":
        raise AssertionError(json.dumps(partial_result, ensure_ascii=False, indent=2))
    partial_events = [
        item
        for item in _read_jsonl(runs_root / "run_demo" / "event_log.jsonl")
        if item.get("bridge_window_id") == partial_packet["binding"]["bridge_window_id"]
    ]
    partial_event_kinds = [item.get("event_kind") for item in partial_events]
    if "partial_evidence_collected" not in partial_event_kinds or "wait_timeout_or_process_lost" in partial_event_kinds:
        raise AssertionError(json.dumps(partial_event_kinds, ensure_ascii=False, indent=2))

    running_partial_packet = decide_next_bridge_packet(
        str(control_root),
        "run_demo",
        runtime_runs_root=str(runs_root),
        main_session_id="main_demo",
        user_instruction="run l4 execute running partial protocol smoke",
        task_spec={"task_subject": "sdk bridge running partial smoke", "task_kind": "bridge_window_smoke"},
        target_phase="l4_execute",
    )
    running_partial_result = call_bridge_sdk(
        str(control_root),
        running_partial_packet,
        runtime_runs_root=str(runs_root),
        persist=True,
        team_executor=lambda _: {
            "status": "partial",
            "waiting": True,
            "wait_reason": "process_running",
            "owned_process_refs": [{"pid": 12345, "status": "running", "log_path": "train.log"}],
            "reports": [report("training still running")],
            "artifact_refs": [],
            "evidence": {"process_status": "running"},
        },
    )
    if running_partial_result.get("status") != "failed":
        raise AssertionError(json.dumps(running_partial_result, ensure_ascii=False, indent=2))
    if running_partial_result.get("error_or_null", {}).get("type") != "L4ExecutePrematurePartialReturn":
        raise AssertionError(json.dumps(running_partial_result, ensure_ascii=False, indent=2))
    process_events = _read_jsonl(run_root / "process_events.jsonl")
    if not any(item.get("state") == "running" and item.get("pid") == 12345 for item in process_events):
        raise AssertionError(json.dumps(process_events, ensure_ascii=False, indent=2))

    reject_packet = decide_next_bridge_packet(
        str(control_root),
        "run_demo",
        runtime_runs_root=str(runs_root),
        main_session_id="main_demo",
        user_instruction="run rejected completion sdk bridge smoke task",
        task_spec={"task_subject": "sdk bridge reject smoke", "task_kind": "bridge_window_smoke"},
    )
    reject_packet["completion_contract"]["required_artifacts"] = ["artifact", "log_manifest"]
    reject_packet["completion_contract"]["success_criteria"] = ["artifact required"]
    reject_result = call_bridge_sdk(
        str(control_root),
        reject_packet,
        runtime_runs_root=str(runs_root),
        persist=True,
        team_executor=lambda _: {
            "status": "succeeded",
            "reports": [report("missing required artifact")],
            "artifact_refs": [],
            "evidence": {"classification": "intentional missing artifact"},
            "error_or_null": None,
            "cleanup_required": False,
        },
    )
    if reject_result.get("status") != "failed":
        raise AssertionError(json.dumps(reject_result, ensure_ascii=False, indent=2))

    manifest_packet = decide_next_bridge_packet(
        str(control_root),
        "run_demo",
        runtime_runs_root=str(runs_root),
        main_session_id="main_demo",
        user_instruction="run l4 execute manifest contract smoke task",
        task_spec={"task_subject": "sdk bridge manifest smoke", "task_kind": "bridge_window_smoke"},
        target_phase="l4_execute",
    )
    manifest_result = call_bridge_sdk(
        str(control_root),
        manifest_packet,
        runtime_runs_root=str(runs_root),
        persist=True,
        team_executor=lambda _: {
            "status": "succeeded",
            "reports": [
                {
                    "summary": "manifest present",
                    "instruction_coverage": coverage_for_packet(manifest_packet),
                    "semantic_identity_resolution": {"disposition": "not_applicable", "basis": "smoke fixture"},
                    "evidence_refs": ["logs/runs/demo/artifact_manifest.json"],
                    "manifest required fields checklist": {
                        "run_id": "present",
                        "bridge_window_id": "present",
                        "task_id": "present",
                        "command": "present",
                        "cwd": "present",
                        "batchbasis": "present",
                        "gpu_id": "present",
                        "memory observed": "present",
                        "model": "present",
                        "dataset": "present",
                        "method": "present",
                    },
                }
            ],
            "artifact_refs": ["logs/runs/demo/artifact_manifest.json"],
            "evidence": {
                "classification": "manifest present",
                "manifest_required_fields_checklist": {
                    "run_id": "run_demo",
                    "bridge_window_id": manifest_packet["binding"]["bridge_window_id"],
                    "task_id": "task_demo",
                    "command": "conda run -n mjy torchrun train.py --ckpt ckpt/demo --batch_size 8",
                    "cwd": ".",
                    "batchbasis": "smoke-derived final batch 8",
                    "gpu_id": "0",
                    "memory observed": "smoke memory observed 12GB; warmup memory observed 72GB",
                    "model": "demo model",
                    "dataset": "demo dataset",
                    "method": "DPO",
                },
            },
            "error_or_null": None,
            "cleanup_required": False,
        },
    )
    if manifest_result.get("status") != "succeeded":
        raise AssertionError(json.dumps(manifest_result, ensure_ascii=False, indent=2))

    replay = reconcile_workflow_from_ledger(str(control_root), "run_demo", runtime_runs_root=str(runs_root), persist=True)
    failed_status = replay["runtime_snapshot"]["lifecycle"]["status_index"][failed_packet["binding"]["bridge_window_id"]]
    exception_status = replay["runtime_snapshot"]["lifecycle"]["status_index"][exception_packet["binding"]["bridge_window_id"]]
    partial_status = replay["runtime_snapshot"]["lifecycle"]["status_index"][partial_packet["binding"]["bridge_window_id"]]
    running_partial_status = replay["runtime_snapshot"]["lifecycle"]["status_index"][running_partial_packet["binding"]["bridge_window_id"]]
    reject_status = replay["runtime_snapshot"]["lifecycle"]["status_index"][reject_packet["binding"]["bridge_window_id"]]
    manifest_status = replay["runtime_snapshot"]["lifecycle"]["status_index"][manifest_packet["binding"]["bridge_window_id"]]
    return {
        "bridge_window_id": packet["binding"]["bridge_window_id"],
        "bridge_result_status": bridge_result["status"],
        "replay_status": status,
        "failed_status": failed_status,
        "exception_status": exception_status,
        "partial_status": partial_status,
        "running_partial_status": running_partial_status,
        "reject_status": reject_status,
        "manifest_status": manifest_status,
        "replayed_events": replay["source_summary"]["event_count"],
    }


def assert_denied(control_root: Path, runs_root: Path, payload: dict, expected_reason: str) -> None:
    result = dispatch_workflow_event(str(control_root), payload, runtime_runs_root=str(runs_root), persist=True)
    reasons = result.check_result.get("reasons", [])
    if result.ok or expected_reason not in reasons:
        raise AssertionError(json.dumps({"expected": expected_reason, "actual": result.check_result}, ensure_ascii=False, indent=2))


def run_negative_tests(control_root: Path, runs_root: Path) -> dict:
    bad_semantic = packet("bw_bad_semantic", "sub_bad_semantic")
    bad_semantic["frozen_semantics"] = {"goal": "drifted"}
    assert_denied(
        control_root,
        runs_root,
        event("bridge_call_intended", "bw_bad_semantic", "sub_bad_semantic", agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_bad_semantic", payload={"packet": bad_semantic}),
        "bridge_packet_frozen_semantics_mismatch",
    )

    weak_contract = packet("bw_weak_contract", "sub_weak_contract")
    weak_contract["completion_contract"]["required_outputs"] = []
    weak_contract["task_spec"]["completion_contract"] = weak_contract["completion_contract"]
    assert_denied(
        control_root,
        runs_root,
        event("bridge_call_intended", "bw_weak_contract", "sub_weak_contract", agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_weak_contract", payload={"packet": weak_contract}),
        "bridge_packet_completion_contract_not_policy_owned",
    )

    missing_semantic_contract = packet("bw_missing_semantic_contract", "sub_missing_semantic_contract")
    missing_semantic_contract["task_spec"].pop("semantic_resolution_contract", None)
    assert_denied(
        control_root,
        runs_root,
        event("bridge_call_intended", "bw_missing_semantic_contract", "sub_missing_semantic_contract", agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_missing_semantic_contract", payload={"packet": missing_semantic_contract}),
        "bridge_packet_missing_semantic_resolution_contract",
    )

    caller_approval = packet("bw_caller_approval", "sub_caller_approval")
    caller_approval["approval_requirements"] = [{"reason": "caller supplied approval policy"}]
    assert_denied(
        control_root,
        runs_root,
        event("bridge_call_intended", "bw_caller_approval", "sub_caller_approval", agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_caller_approval", payload={"packet": caller_approval}),
        "bridge_packet_approval_requirements_not_runtime_owned",
    )

    bad_l3_scope = packet("bw_bad_l3_scope", "sub_bad_l3_scope")
    bad_l3_scope["target_phase"] = "l3_bridge"
    bad_l3_scope["phase_route"] = ["l3_bridge"]
    bad_l3_scope["team_spec"]["ownership_boundary"]["writable_scopes"] = ["CLAUDE.md", "README.md", "docs/", "*.md"]
    assert_denied(
        control_root,
        runs_root,
        event("bridge_call_intended", "bw_bad_l3_scope", "sub_bad_l3_scope", agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_bad_l3_scope", payload={"packet": bad_l3_scope}),
        "bridge_packet_l3_write_scope_not_policy_owned",
    )

    hardened_l3 = decide_next_bridge_packet(
        str(control_root),
        "run_demo",
        runtime_runs_root=str(runs_root),
        main_session_id="main_demo",
        user_instruction="l3 documentation scope smoke; update CLAUDE.md; require explicit no-op reason",
        task_spec={
            "task_subject": "l3 instruction preservation smoke",
            "requirements": ["preserve every user requirement", "report checklist disposition"],
            "context_note": "extra context must survive normalization",
        },
        target_phase="l3_bridge",
    )
    l3_task = hardened_l3["task_spec"]
    l3_checklist = set(l3_task.get("instruction_coverage_checklist", []))
    if not {
        "preserve every user requirement",
        "report checklist disposition",
        "l3 documentation scope smoke",
        "update CLAUDE.md",
        "require explicit no-op reason",
    }.issubset(l3_checklist):
        raise AssertionError(json.dumps(l3_task, ensure_ascii=False, indent=2))
    if l3_task.get("preserved_task_context", {}).get("context_note") != "extra context must survive normalization":
        raise AssertionError(json.dumps(l3_task, ensure_ascii=False, indent=2))
    if "l3 documentation scope smoke" not in str(l3_task.get("current_user_intent_context", {}).get("active_user_intent", "")):
        raise AssertionError(json.dumps(l3_task.get("current_user_intent_context"), ensure_ascii=False, indent=2))
    if "instruction_coverage" not in set(hardened_l3["report_contract"].get("required_sections", [])):
        raise AssertionError(json.dumps(hardened_l3["report_contract"], ensure_ascii=False, indent=2))
    if "instruction coverage disposition" not in set(hardened_l3["report_contract"].get("required_evidence", [])):
        raise AssertionError(json.dumps(hardened_l3["report_contract"], ensure_ascii=False, indent=2))
    if "semantic_identity_resolution" not in set(hardened_l3["report_contract"].get("required_sections", [])):
        raise AssertionError(json.dumps(hardened_l3["report_contract"], ensure_ascii=False, indent=2))
    if "semantic identity resolution" not in set(hardened_l3["report_contract"].get("required_evidence", [])):
        raise AssertionError(json.dumps(hardened_l3["report_contract"], ensure_ascii=False, indent=2))
    if "current_user_intent_context" not in set(hardened_l3["report_contract"].get("required_sections", [])):
        raise AssertionError(json.dumps(hardened_l3["report_contract"], ensure_ascii=False, indent=2))
    semantic_fields = set(l3_task.get("semantic_resolution_contract", {}).get("required_identity_fields", []))
    if not {"checkpoint_identity", "dataset_identity", "prompt_or_template_identity"}.issubset(semantic_fields):
        raise AssertionError(json.dumps(l3_task.get("semantic_resolution_contract"), ensure_ascii=False, indent=2))
    l3_boundary = hardened_l3["team_spec"]["ownership_boundary"]
    if set(l3_boundary.get("writable_scopes", [])) != {"."}:
        raise AssertionError(json.dumps(l3_boundary, ensure_ascii=False, indent=2))
    l3_surface_policy = "\n".join(str(item) for item in l3_boundary.get("active_surface_policy", []))
    if "minimum viable" not in l3_surface_policy or "Archive or move" not in l3_surface_policy:
        raise AssertionError(json.dumps(l3_boundary, ensure_ascii=False, indent=2))
    l3_assignments = "\n".join(
        str(item.get("assignment") or "")
        for item in hardened_l3.get("task_team_mapping", {}).get("teammate_assignments", [])
        if isinstance(item, dict)
    )
    required_l3_assignment_markers = [
        "CLAUDE.md",
        "L3 curator Bash curation rule",
        "Active surface policy",
        "active code, log, checkpoint, data, document, and script surfaces minimum viable",
        "Instruction coverage checklist",
        "Semantic resolution contract",
        "Current user intent context",
        "L3 current-intent bridge rule",
        "confirmed, refined, superseded, blocked, or escalated",
        "checkpoint",
        "dataset",
        "prompt",
        "inherit the current active dataset/prompt/config basis",
        "do not mark the task complete until every checklist item is completed",
        "extra context must survive normalization",
    ]
    if any(marker not in l3_assignments for marker in required_l3_assignment_markers):
        raise AssertionError(json.dumps(hardened_l3.get("task_team_mapping"), ensure_ascii=False, indent=2))
    l3_team_tools = {
        tool
        for teammate in hardened_l3["team_spec"]["teammate_specs"]
        for tool in teammate.get("allowed_tools", [])
    }
    if "Write" not in set(hardened_l3.get("allowed_tools", [])) or "Write" not in l3_team_tools:
        raise AssertionError(json.dumps(hardened_l3["team_spec"], ensure_ascii=False, indent=2))
    if "Bash" not in set(hardened_l3.get("allowed_tools", [])):
        raise AssertionError(json.dumps(hardened_l3["team_spec"], ensure_ascii=False, indent=2))
    l3_tools_by_name = {
        teammate.get("teammate_name"): set(teammate.get("allowed_tools", []))
        for teammate in hardened_l3["team_spec"]["teammate_specs"]
    }
    if "Bash" not in l3_tools_by_name.get("curator", set()):
        raise AssertionError(json.dumps(hardened_l3["team_spec"], ensure_ascii=False, indent=2))
    for no_shell_teammate in ["preflight-initial", "refresher"]:
        if "Bash" in l3_tools_by_name.get(no_shell_teammate, set()):
            raise AssertionError(json.dumps(hardened_l3["team_spec"], ensure_ascii=False, indent=2))
    hardened_l3_bw = hardened_l3["binding"]["bridge_window_id"]
    hardened_l3_sub = hardened_l3["binding"]["sub_session_id"]
    hardened_l3_tool = hardened_l3["binding"]["parent_tool_use_id"]
    dispatch(
        control_root,
        runs_root,
        event(
            "bridge_call_intended",
            hardened_l3_bw,
            hardened_l3_sub,
            agent_type="main-leader",
            agent_id="main",
            tool_name="call_bridge_sdk",
            tool_use_id=hardened_l3_tool,
            payload={"packet": hardened_l3},
        ),
    )
    dispatch(
        control_root,
        runs_root,
        event(
            "pretooluse_denied_by_main_leader",
            hardened_l3_bw,
            hardened_l3_sub,
            agent_type="main-leader",
            agent_id="main",
            tool_name="call_bridge_sdk",
            tool_use_id=hardened_l3_tool,
            payload={"reasons": ["l3 documentation scope smoke cleanup"]},
        ),
    )

    leader_freeze_packet = decide_next_bridge_packet(
        str(control_root),
        "run_demo",
        runtime_runs_root=str(runs_root),
        main_session_id="main_demo",
        user_instruction="return to leader freeze smoke",
        target_phase="leader_freeze",
    )
    leader_freeze_bw = leader_freeze_packet["binding"]["bridge_window_id"]
    leader_freeze_sub = leader_freeze_packet["binding"]["sub_session_id"]
    leader_freeze_tool = leader_freeze_packet["binding"]["parent_tool_use_id"]
    dispatch(
        control_root,
        runs_root,
        event(
            "bridge_call_intended",
            leader_freeze_bw,
            leader_freeze_sub,
            agent_type="main-leader",
            agent_id="main",
            tool_name="call_bridge_sdk",
            tool_use_id=leader_freeze_tool,
            payload={"packet": leader_freeze_packet},
        ),
    )
    dispatch(
        control_root,
        runs_root,
        event(
            "pretooluse_denied_by_main_leader",
            leader_freeze_bw,
            leader_freeze_sub,
            agent_type="main-leader",
            agent_id="main",
            tool_name="call_bridge_sdk",
            tool_use_id=leader_freeze_tool,
            payload={"reasons": ["leader freeze route smoke cleanup"]},
        ),
    )

    hardened_implement = decide_next_bridge_packet(
        str(control_root),
        "run_demo",
        runtime_runs_root=str(runs_root),
        main_session_id="main_demo",
        user_instruction="policy hardening smoke",
        target_phase="l4_implement",
    )
    hardened_tools = set(hardened_implement.get("allowed_tools", []))
    hardened_team = hardened_implement["team_spec"]
    hardened_team_tools = {
        tool
        for teammate in hardened_team["teammate_specs"]
        for tool in teammate.get("allowed_tools", [])
    }
    if "Write" not in hardened_tools or "Write" not in hardened_team_tools:
        raise AssertionError(json.dumps(hardened_implement, ensure_ascii=False, indent=2))
    if not hardened_team["ownership_boundary"].get("writable_scopes"):
        raise AssertionError(json.dumps(hardened_team["ownership_boundary"], ensure_ascii=False, indent=2))
    implement_surface_policy = "\n".join(str(item) for item in hardened_team["ownership_boundary"].get("active_surface_policy", []))
    if "minimum viable repository surface" not in implement_surface_policy:
        raise AssertionError(json.dumps(hardened_team["ownership_boundary"], ensure_ascii=False, indent=2))
    implement_assignments = "\n".join(
        str(item.get("assignment") or "")
        for item in hardened_implement.get("task_team_mapping", {}).get("teammate_assignments", [])
        if isinstance(item, dict)
    )
    if "Minimum-viable repository rule" not in implement_assignments or "Gate the repository surface" not in implement_assignments or "do not silently swap checkpoint, dataset, prompt" not in implement_assignments:
        raise AssertionError(json.dumps(hardened_implement.get("task_team_mapping"), ensure_ascii=False, indent=2))
    hardened_bw = hardened_implement["binding"]["bridge_window_id"]
    hardened_sub = hardened_implement["binding"]["sub_session_id"]
    hardened_tool = hardened_implement["binding"]["parent_tool_use_id"]
    dispatch(
        control_root,
        runs_root,
        event(
            "bridge_call_intended",
            hardened_bw,
            hardened_sub,
            agent_type="main-leader",
            agent_id="main",
            tool_name="call_bridge_sdk",
            tool_use_id=hardened_tool,
            payload={"packet": hardened_implement},
        ),
    )
    dispatch(
        control_root,
        runs_root,
        event(
            "pretooluse_denied_by_main_leader",
            hardened_bw,
            hardened_sub,
            agent_type="main-leader",
            agent_id="main",
            tool_name="call_bridge_sdk",
            tool_use_id=hardened_tool,
            payload={"reasons": ["policy hardening smoke cleanup"]},
        ),
    )

    l2_packet = decide_next_bridge_packet(
        str(control_root),
        "run_demo",
        runtime_runs_root=str(runs_root),
        main_session_id="main_demo",
        user_instruction="three-seat l2 advisory smoke",
        target_phase="l2_advisory",
    )
    l2_names = [item.get("teammate_name") for item in l2_packet["team_spec"].get("teammate_specs", [])]
    if l2_names != ["chiefmate-a", "chiefmate-b", "chiefmate-c"]:
        raise AssertionError(json.dumps(l2_packet["team_spec"], ensure_ascii=False, indent=2))
    l2_tools = {
        tool
        for teammate in l2_packet["team_spec"]["teammate_specs"]
        for tool in teammate.get("allowed_tools", [])
    }
    if "WebSearch" not in l2_tools or "WebFetch" not in l2_tools:
        raise AssertionError(json.dumps(l2_packet["team_spec"], ensure_ascii=False, indent=2))
    l2_required_sections = set(l2_packet["report_contract"].get("required_sections", []))
    l2_required_evidence = set(l2_packet["report_contract"].get("required_evidence", []))
    if "major_technical_plan_pseudocode" not in l2_required_sections or "pseudocode flow for each new major technical plan or explicit not_applicable reason" not in l2_required_evidence:
        raise AssertionError(json.dumps(l2_packet["report_contract"], ensure_ascii=False, indent=2))
    l2_assignments = json.dumps(l2_packet["task_team_mapping"]["teammate_assignments"], ensure_ascii=False)
    if (
        "chiefmate-c" not in l2_assignments
        or "L2 three-seat review rule" not in l2_assignments
        or "L2 factual confidence loop" not in l2_assignments
        or "L2 research rule" not in l2_assignments
        or "L2 pseudocode rule" not in l2_assignments
    ):
        raise AssertionError(l2_assignments)

    anomaly_packet = decide_next_bridge_packet(
        str(control_root),
        "run_demo",
        runtime_runs_root=str(runs_root),
        main_session_id="main_demo",
        user_instruction="three-seat anomaly smoke",
        target_phase="l4_anomaly",
    )
    anomaly_names = [item.get("teammate_name") for item in anomaly_packet["team_spec"].get("teammate_specs", [])]
    if anomaly_names != ["anomaly-analyst-a", "anomaly-analyst-b", "anomaly-analyst-c"]:
        raise AssertionError(json.dumps(anomaly_packet["team_spec"], ensure_ascii=False, indent=2))
    anomaly_tools = {
        tool
        for teammate in anomaly_packet["team_spec"]["teammate_specs"]
        for tool in teammate.get("allowed_tools", [])
    }
    if "WebSearch" not in anomaly_tools or "WebFetch" not in anomaly_tools:
        raise AssertionError(json.dumps(anomaly_packet["team_spec"], ensure_ascii=False, indent=2))
    anomaly_responsibilities = [
        tuple(item.get("responsibilities", []))
        for item in anomaly_packet["team_spec"].get("teammate_specs", [])
    ]
    anomaly_responsibilities_text = [" ".join(items) for items in anomaly_responsibilities]
    if not all("complete independent anomaly diagnosis before peer review" in text for text in anomaly_responsibilities_text):
        raise AssertionError(json.dumps(anomaly_packet["team_spec"], ensure_ascii=False, indent=2))
    if "rebutting weak peer causal convergence" not in anomaly_responsibilities_text[-1]:
        raise AssertionError(json.dumps(anomaly_packet["team_spec"], ensure_ascii=False, indent=2))
    anomaly_assignments = json.dumps(anomaly_packet["task_team_mapping"]["teammate_assignments"], ensure_ascii=False)
    if (
        "anomaly-analyst-c" not in anomaly_assignments
        or "L4 anomaly no-preassigned-lane rule" not in anomaly_assignments
        or "complete independent diagnosis from the full packet context" not in anomaly_assignments
        or "original answers, outputs, predictions, traces, or result samples" not in anomaly_assignments
        or "peer agreement does not prove causality" not in anomaly_assignments
    ):
        raise AssertionError(anomaly_assignments)

    execute_packet = decide_next_bridge_packet(
        str(control_root),
        "run_demo",
        runtime_runs_root=str(runs_root),
        main_session_id="main_demo",
        user_instruction="long formal execution timeout smoke",
        target_phase="l4_execute",
    )
    execute_timeout = execute_packet["completion_contract"]["timeout_policy"]
    if execute_timeout.get("soft_timeout_seconds", 0) < 21600 or execute_timeout.get("hard_timeout_seconds", 0) < 86400:
        raise AssertionError(json.dumps(execute_timeout, ensure_ascii=False, indent=2))
    if execute_timeout.get("wait_until_process_complete") is not True or execute_timeout.get("partial_return_allowed_only_after_process_terminal") is not True:
        raise AssertionError(json.dumps(execute_timeout, ensure_ascii=False, indent=2))
    if execute_packet["task_spec"]["completion_contract"]["timeout_policy"] != execute_timeout:
        raise AssertionError(json.dumps(execute_packet["task_spec"]["completion_contract"], ensure_ascii=False, indent=2))
    if "log_manifest" not in set(execute_packet["completion_contract"].get("required_artifacts", [])):
        raise AssertionError(json.dumps(execute_packet["completion_contract"], ensure_ascii=False, indent=2))
    if "artifact_manifests" not in set(execute_packet["report_contract"].get("required_sections", [])):
        raise AssertionError(json.dumps(execute_packet["report_contract"], ensure_ascii=False, indent=2))
    if "log manifest path" not in set(execute_packet["report_contract"].get("required_evidence", [])):
        raise AssertionError(json.dumps(execute_packet["report_contract"], ensure_ascii=False, indent=2))
    required_execute_evidence = set(execute_packet["report_contract"].get("required_evidence", []))
    for required_manifest_evidence in [
        "manifest required fields checklist",
        "batchbasis",
        "gpu_id",
        "smoke memory observed when smoke ran",
        "warmup memory observed when warmup ran",
        "natural-language model dataset method semantics",
    ]:
        if required_manifest_evidence not in required_execute_evidence:
            raise AssertionError(json.dumps(execute_packet["report_contract"], ensure_ascii=False, indent=2))
    execute_assignments = "\n".join(
        str(item.get("assignment") or "")
        for item in execute_packet.get("task_team_mapping", {}).get("teammate_assignments", [])
        if isinstance(item, dict)
    )
    required_execute_assignment_markers = [
        "estimated wall-clock runtime range",
        "do not return final or partial while an owned process is still running",
        "bounded smoke evidence",
        "Each formal stage must independently satisfy batch/memory adaptation",
        "effective batch size",
        "Formal log manifest required fields",
        "batchbasis",
        "warmup_memory_observed",
        "dataset_name_split_source",
    ]
    if any(marker not in execute_assignments for marker in required_execute_assignment_markers):
        raise AssertionError(json.dumps(execute_packet.get("task_team_mapping"), ensure_ascii=False, indent=2))
    execute_assignments = json.dumps(execute_packet["task_team_mapping"]["teammate_assignments"], ensure_ascii=False)
    if (
        "conda env mjy" not in execute_assignments
        or "forbidden_formal_environments" not in execute_assignments
        or "typical_80gb_gpu_min_observed_gb" not in execute_assignments
        or "other_gpu_min_observed_fraction" not in execute_assignments
        or "multi_stage_memory_evidence_required" not in execute_assignments
        or "Environment and GPU audit rule" not in execute_assignments
        or "Semantic audit rule" not in execute_assignments
        or "Log manifest audit rule" not in execute_assignments
    ):
        raise AssertionError(execute_assignments)

    bad_implement = packet("bw_bad_implement", "sub_bad_implement")
    bad_implement["target_phase"] = "l4_implement"
    bad_implement["phase_route"] = ["l4_implement"]
    bad_implement["allowed_tools"] = ["Read", "Grep", "Glob"]
    bad_implement["team_spec"]["teammate_specs"] = [
        {
            "teammate_id_or_null": None,
            "teammate_name": "implementor",
            "role": "implement",
            "allowed_tools": ["Read", "Grep", "Glob"],
            "responsibilities": ["attempt implementation without write authority"],
        }
    ]
    bad_implement["team_spec"]["ownership_boundary"]["writable_scopes"] = []
    assert_denied(
        control_root,
        runs_root,
        event("bridge_call_intended", "bw_bad_implement", "sub_bad_implement", agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_bad_implement", payload={"packet": bad_implement}),
        "bridge_packet_implement_requires_write_authority",
    )

    open_packet = packet("bw_open", "sub_open")
    dispatch(control_root, runs_root, event("bridge_call_intended", "bw_open", "sub_open", agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_open", payload={"packet": open_packet}))
    assert_denied(
        control_root,
        runs_root,
        event("bridge_call_intended", "bw_second", "sub_second", agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_second", payload={"packet": packet("bw_second", "sub_second")}),
        "bridge_call_not_allowed_in_current_phase",
    )
    dispatch(control_root, runs_root, event("pretooluse_denied_by_main_leader", "bw_open", "sub_open", agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_open", payload={"reasons": ["negative cleanup"]}))

    stray_failure = dispatch_workflow_event(
        str(control_root),
        event("bridge_leader_fails_task", "bw_stray_failure", "sub_stray_failure", team_id="team_stray", task_id="task_stray", payload={"error_or_null": {"message": "stray"}}),
        runtime_runs_root=str(runs_root),
        persist=True,
    )
    if stray_failure.ok or "lifecycle_transition_not_allowed" not in stray_failure.check_result.get("reasons", []):
        raise AssertionError(json.dumps(stray_failure.check_result, ensure_ascii=False, indent=2))
    stray_snapshot = stray_failure.runtime_snapshot
    if "bw_stray_failure" in stray_snapshot.get("lifecycle", {}).get("status_index", {}):
        raise AssertionError(json.dumps(stray_snapshot["lifecycle"], ensure_ascii=False, indent=2))
    transition_ids = [item.get("transition_id") for item in _read_jsonl(runs_root / "run_demo" / "transitions.jsonl")]
    if len(transition_ids) != len(set(transition_ids)):
        raise AssertionError(json.dumps(transition_ids, ensure_ascii=False, indent=2))

    no_contract = packet("bw_no_contract", "sub_no_contract")
    dispatch(control_root, runs_root, event("bridge_call_intended", "bw_no_contract", "sub_no_contract", agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_no_contract", payload={"packet": no_contract}))
    dispatch(control_root, runs_root, event("pretooluse_allowed_by_main_leader", "bw_no_contract", "sub_no_contract", agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_no_contract", payload={"packet": no_contract}))
    dispatch(control_root, runs_root, event("call_bridge_sdk_started", "bw_no_contract", "sub_no_contract", agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_no_contract", payload={"packet": no_contract}))
    dispatch(control_root, runs_root, event("bridge_window_opened", "bw_no_contract", "sub_no_contract", agent_type="bridge-leader", payload={"packet": no_contract}))
    dispatch(control_root, runs_root, event("bridge_packet_accepted", "bw_no_contract", "sub_no_contract", payload={"packet": no_contract}))
    dispatch(control_root, runs_root, event("team_create_started", "bw_no_contract", "sub_no_contract", team_id="team_no_contract", tool_name="team_create"))
    dispatch(control_root, runs_root, event("team_create_succeeded", "bw_no_contract", "sub_no_contract", team_id="team_no_contract", tool_name="team_create", payload={"team_name": "team_no_contract", "teammate_ids": ["mate_1"]}))
    dispatch(control_root, runs_root, event("task_create_started", "bw_no_contract", "sub_no_contract", team_id="team_no_contract", task_id="task_no_contract", tool_name="task_create"))
    dispatch(control_root, runs_root, event("task_create_succeeded", "bw_no_contract", "sub_no_contract", team_id="team_no_contract", task_id="task_no_contract", tool_name="task_create"))
    dispatch(
        control_root,
        runs_root,
        event(
            "taskcreated_hook_accepted",
            "bw_no_contract",
            "sub_no_contract",
            team_id="team_no_contract",
            task_id="task_no_contract",
            agent_type="hook",
            agent_id="hook.task_created",
            payload={
                "task_subject": "task_bw_no_contract",
                "task_description": "smoke task created",
                "task_spec": no_contract["task_spec"],
                "team_spec": no_contract["team_spec"],
                "task_team_mapping": no_contract["task_team_mapping"],
            },
        ),
    )
    dispatch(control_root, runs_root, event("message_dispatch_started", "bw_no_contract", "sub_no_contract", team_id="team_no_contract", task_id="task_no_contract", tool_name="send_messages"))
    dispatch(control_root, runs_root, event("message_dispatch_succeeded", "bw_no_contract", "sub_no_contract", team_id="team_no_contract", task_id="task_no_contract", tool_name="send_messages"))
    dispatch(control_root, runs_root, event("artifacts_ready", "bw_no_contract", "sub_no_contract", team_id="team_no_contract", task_id="task_no_contract", tool_name="task_complete"))
    assert_denied(
        control_root,
        runs_root,
        event("completion_contract_satisfied", "bw_no_contract", "sub_no_contract", team_id="team_no_contract", task_id="task_no_contract", agent_type="hook", agent_id="hook.task_completed", payload={"completion_checks": {}}),
        "completion_contract_missing",
    )
    return {"negative_tests": "passed"}


def run_cli_executor_policy_tests(root: Path) -> dict:
    wrapped_stdout = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "result": json.dumps(
                {
                    "status": "succeeded",
                    "reports": [report()],
                    "artifact_refs": [],
                    "evidence": {"completion_contract": "satisfied"},
                    "error_or_null": None,
                    "cleanup_required": False,
                }
            ),
        }
    )
    parsed = _parse_claude_payload(wrapped_stdout, "")
    if parsed.get("error_or_null") or parsed.get("payload", {}).get("status") != "succeeded":
        raise AssertionError(json.dumps(parsed, ensure_ascii=False, indent=2))

    ndjson_stdout = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "result": json.dumps(
                        {
                            "status": "succeeded",
                            "reports": [report("ndjson ok")],
                            "artifact_refs": [],
                            "evidence": {"completion_contract": "satisfied"},
                            "error_or_null": None,
                            "cleanup_required": False,
                        }
                    ),
                }
            ),
        ]
    )
    ndjson_envelope = _parse_claude_stdout_envelope(ndjson_stdout)
    if not isinstance(ndjson_envelope, dict) or ndjson_envelope.get("type") != "result":
        raise AssertionError(json.dumps(ndjson_envelope, ensure_ascii=False, indent=2))
    ndjson_parsed = _parse_claude_payload(ndjson_stdout, "")
    if ndjson_parsed.get("error_or_null") or ndjson_parsed.get("payload", {}).get("reports", [{}])[0].get("summary") != "ndjson ok":
        raise AssertionError(json.dumps(ndjson_parsed, ensure_ascii=False, indent=2))

    empty_result = _parse_claude_payload(
        json.dumps({"type": "result", "subtype": "success", "result": ""}),
        "",
    )
    if empty_result.get("error_or_null", {}).get("type") != "ClaudeCliEmptyResult":
        raise AssertionError(json.dumps(empty_result, ensure_ascii=False, indent=2))
    if "envelope" not in empty_result.get("evidence", {}):
        raise AssertionError(json.dumps(empty_result, ensure_ascii=False, indent=2))

    tool_use_result = _parse_claude_payload(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "stop_reason": "tool_use",
                "result": "",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_demo",
                        "name": "Read",
                        "input": {"file_path": "README.md"},
                    }
                ],
            }
        ),
        "",
    )
    tool_use_payload = tool_use_result.get("payload", {})
    if tool_use_result.get("error_or_null") or tool_use_payload.get("status") != "partial_or_failed":
        raise AssertionError(json.dumps(tool_use_result, ensure_ascii=False, indent=2))
    if tool_use_payload.get("error_or_null", {}).get("type") != "ClaudeCliNeedsToolContinuation":
        raise AssertionError(json.dumps(tool_use_result, ensure_ascii=False, indent=2))
    evidence = tool_use_payload.get("evidence", {})
    if evidence.get("stop_reason") != "tool_use" or not evidence.get("pending_tool_uses"):
        raise AssertionError(json.dumps(tool_use_result, ensure_ascii=False, indent=2))

    redacted_cmd = _redact_cmd(["claude", "-p", "--model", "gpt-main", "--", "bridge prompt body"])
    if "bridge prompt body" in redacted_cmd or "<prompt:18 chars>" not in redacted_cmd:
        raise AssertionError(json.dumps(redacted_cmd, ensure_ascii=False, indent=2))

    stream_json_args = _claude_print_stream_json_args()
    if "--output-format" not in stream_json_args or "stream-json" not in stream_json_args or "--verbose" not in stream_json_args or "--include-partial-messages" not in stream_json_args:
        raise AssertionError(json.dumps(stream_json_args, ensure_ascii=False, indent=2))

    cli_packet = packet("bw_cli_policy", "sub_cli_policy")
    cli_packet["allowed_tools"] = ["Agent", "Read", "Grep", "Glob", "LS"]
    cli_packet["team_spec"]["teammate_specs"] = [
        {"teammate_name": "curator", "role": "curate", "allowed_tools": ["Read", "Write"], "responsibilities": []},
        {"teammate_name": "preflight-initial", "role": "preflight", "allowed_tools": ["Read"], "responsibilities": []},
        {"teammate_name": "refresher", "role": "refresh", "allowed_tools": ["Read", "Write"], "responsibilities": []},
    ]
    allowed_tools = _allowed_tools(cli_packet)
    expected_agent_tool = "Agent(curator,preflight-initial,refresher)"
    if expected_agent_tool not in allowed_tools or "Agent" in allowed_tools:
        raise AssertionError(json.dumps(allowed_tools, ensure_ascii=False, indent=2))

    project_root = root / "project_agent_validation"
    sync_result = _ensure_project_agent_files(project_root, ["bridge-leader", "curator", "preflight-initial", "refresher"])
    if sync_result.get("error_or_null"):
        raise AssertionError(json.dumps(sync_result, ensure_ascii=False, indent=2))
    for name in ["bridge-leader", "curator", "preflight-initial", "refresher"]:
        agent_path = Path(sync_result["source_dir"]) / f"{name}.md"
        text = agent_path.read_text(encoding="utf-8")
        if "model: gpt-main" not in text:
            raise AssertionError(str(agent_path))
    if (project_root / ".claude" / "agents").exists():
        raise AssertionError(str(project_root / ".claude" / "agents"))

    old_settings = os.environ.pop("BRIDGE_CLAUDE_SETTINGS", None)
    try:
        settings_args = _settings_args()
    finally:
        if old_settings is not None:
            os.environ["BRIDGE_CLAUDE_SETTINGS"] = old_settings
    if not settings_args or settings_args[0] != "--settings":
        raise AssertionError(json.dumps(settings_args, ensure_ascii=False, indent=2))
    generated_settings = Path(settings_args[1])
    settings_payload = json.loads(generated_settings.read_text(encoding="utf-8"))
    hooks = settings_payload.get("hooks", {}) if isinstance(settings_payload, dict) else {}
    for event_name in ["SessionStart", "SubagentStart", "PreToolUse", "PostToolUse"]:
        if event_name not in hooks:
            raise AssertionError(json.dumps(settings_payload, ensure_ascii=False, indent=2))
    settings_text = generated_settings.read_text(encoding="utf-8")
    if "../.claude/hooks" in settings_text or ".claude/hooks/" in settings_text:
        raise AssertionError(settings_text)

    old_allowed_models = os.environ.get("BRIDGE_ALLOWED_MODELS")
    os.environ["BRIDGE_ALLOWED_MODELS"] = "gpt-main,sonnet-main,deepseek-main"
    try:
        mixed_model_result = _required_agent_models(
            [
                "bridge-leader",
                "chiefmate-a",
                "chiefmate-b",
                "chiefmate-c",
                "anomaly-analyst-a",
                "anomaly-analyst-b",
                "anomaly-analyst-c",
            ]
        )
    finally:
        if old_allowed_models is None:
            os.environ.pop("BRIDGE_ALLOWED_MODELS", None)
        else:
            os.environ["BRIDGE_ALLOWED_MODELS"] = old_allowed_models
    if mixed_model_result.get("error_or_null") or mixed_model_result.get("models", {}).get("chiefmate-b") != "deepseek-main":
        raise AssertionError(json.dumps(mixed_model_result, ensure_ascii=False, indent=2))
    if mixed_model_result.get("models", {}).get("anomaly-analyst-b") != "deepseek-main":
        raise AssertionError(json.dumps(mixed_model_result, ensure_ascii=False, indent=2))

    original_text = "\u4e0a\u4e00\u6b21\u5c1d\u8bd5\u7cfb"
    mojibake_text = original_text.encode("utf-8").decode("gbk")
    prompt_packet = packet("bw_mojibake", "sub_mojibake")
    prompt_packet["task_spec"]["task_description"] = mojibake_text
    prompt = _bridge_leader_prompt(
        prompt_packet,
        {
            "run_id": "run_demo",
            "main_session_id": "main_demo",
            "sub_session_id": "sub_mojibake",
            "bridge_window_id": "bw_mojibake",
            "team_id": "team_mojibake",
            "task_id": "task_mojibake",
        },
        project_root,
    )
    if original_text not in prompt or mojibake_text in prompt:
        raise AssertionError(json.dumps({"original": original_text, "mojibake": mojibake_text, "prompt": prompt}, ensure_ascii=False, indent=2))

    stream_project_root = root / "sdk_stream_project"
    stream_project_root.mkdir(parents=True, exist_ok=True)
    stream_input = {
        "run_id": "run_sdk_stream",
        "main_session_id": "main_sdk_stream",
        "sub_session_id": "sub_sdk_stream",
        "bridge_window_id": "bw_sdk_stream",
        "team_id": "team_sdk_stream",
        "task_id": "task_sdk_stream",
    }
    for stream_path in _sdk_stream_event_paths(stream_project_root, stream_input):
        if stream_path.exists():
            stream_path.unlink()
    stream_script = (
        "import json, sys\n"
        "print(json.dumps({'type':'content_block_delta','delta':{'type':'text_delta','text':'partial token=abc123 sk-demoSECRET12345'}}), flush=True)\n"
        "print(json.dumps({'type':'content_block_delta','delta':{'type':'input_json_delta','partial_json':'{\\\"file_path\\\":\\\"README.md\\\"}'},'content_block':{'type':'tool_use','id':'toolu_delta','name':'Read'}}), flush=True)\n"
        "print(json.dumps({'type':'assistant','content':[{'type':'text','text':'hello token=abc123 sk-demoSECRET12345'}]}), flush=True)\n"
        "print(json.dumps({'type':'tool_use','id':'toolu_1','name':'Read','input':{'file_path':'README.md','limit':10}}), flush=True)\n"
        "print(json.dumps({'type':'result','subtype':'success','result': json.dumps({'status':'succeeded','reports':[{'summary':'ok','instruction_coverage':{'smoke checklist':'completed'},'evidence_refs':['event:smoke_checklist']}],'artifact_refs':[],'evidence':{'completion_contract':'satisfied'},'error_or_null':None,'cleanup_required':False})}), flush=True)\n"
        "print('warning password=abc123', file=sys.stderr, flush=True)\n"
    )
    stream_proc = _run_claude_streaming(
        [sys.executable, "-c", stream_script],
        stream_project_root,
        env=os.environ.copy(),
        timeout=30,
        execution_input=stream_input,
    )
    if stream_proc.returncode != 0 or "warning password=abc123" not in stream_proc.stderr:
        raise AssertionError(json.dumps({"returncode": stream_proc.returncode, "stderr": stream_proc.stderr}, ensure_ascii=False, indent=2))
    parsed_stream_result = _parse_claude_payload(stream_proc.stdout, stream_proc.stderr)
    if parsed_stream_result.get("error_or_null") or parsed_stream_result.get("payload", {}).get("status") != "succeeded":
        raise AssertionError(json.dumps(parsed_stream_result, ensure_ascii=False, indent=2))
    stream_paths = _sdk_stream_event_paths(stream_project_root, stream_input)
    for stream_path in stream_paths:
        if not stream_path.exists():
            raise AssertionError(str(stream_path))
        records = [json.loads(line) for line in stream_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        event_types = {record.get("event_type") for record in records}
        required_event_types = {"sdk_stream_started", "sdk_stream_assistant_text", "sdk_stream_tool_use", "sdk_stream_stderr", "sdk_stream_final"}
        if not required_event_types.issubset(event_types):
            raise AssertionError(json.dumps({"path": str(stream_path), "event_types": sorted(event_types)}, ensure_ascii=False, indent=2))
        delta_records = [record for record in records if record.get("raw_stream_event_type") == "content_block_delta"]
        if not any(record.get("text_delta") for record in delta_records):
            raise AssertionError(json.dumps(delta_records, ensure_ascii=False, indent=2))
        if not any(record.get("input_json_delta") and record.get("tool_name") == "Read" for record in delta_records):
            raise AssertionError(json.dumps(delta_records, ensure_ascii=False, indent=2))
        for record in records:
            for key in [
                "timestamp",
                "event_type",
                "stream_source",
                "run_id",
                "bridge_window_id",
                "team_id",
                "task_id",
                "session_id",
                "agent_type",
                "status",
                "message_preview",
                "payload_keys",
                "raw_stream_event_type",
                "sequence",
                "monotonic_index",
            ]:
                if key not in record:
                    raise AssertionError(json.dumps(record, ensure_ascii=False, indent=2))
            if record.get("run_id") != "run_sdk_stream" or record.get("agent_type") != "bridge-leader":
                raise AssertionError(json.dumps(record, ensure_ascii=False, indent=2))
            preview = str(record.get("message_preview") or "")
            delta_text = str(record.get("text_delta") or "")
            input_json_delta = str(record.get("input_json_delta") or "")
            if "abc123" in preview or "sk-demoSECRET12345" in preview or "abc123" in delta_text or "sk-demoSECRET12345" in delta_text or "abc123" in input_json_delta:
                raise AssertionError(json.dumps(record, ensure_ascii=False, indent=2))
        tool_records = [record for record in records if record.get("event_type") == "sdk_stream_tool_use"]
        if not tool_records or tool_records[0].get("tool_name") != "Read" or "file_path" not in tool_records[0].get("tool_input_keys", []):
            raise AssertionError(json.dumps(tool_records, ensure_ascii=False, indent=2))
    return {"cli_executor_policy": "passed"}


def run_hook_observer_rebind_tests(root: Path, runs_root: Path) -> dict:
    hooks_common = Path(__file__).resolve().parents[2] / "hooks" / "common.py"
    spec = importlib.util.spec_from_file_location("hooks_common_smoke", hooks_common)
    if spec is None or spec.loader is None:
        raise AssertionError(str(hooks_common))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    observer_root = root / "session_observer_rebind"
    observer_root.mkdir(parents=True, exist_ok=True)
    old_runs_root = os.environ.get("BRIDGE_RUNTIME_RUNS_ROOT")
    old_observer_root = os.environ.get("BRIDGE_SESSION_OBSERVER_ROOT")
    old_run_id = os.environ.pop("BRIDGE_RUN_ID", None)
    old_control_run_id = os.environ.pop("CLAUDE_CONTROL_RUN_ID", None)
    old_child = os.environ.pop("BRIDGE_CHILD_CLAUDE_SESSION", None)
    rebound_env_keys = [
        "BRIDGE_MAIN_SESSION_ID",
        "CLAUDE_CONTROL_MAIN_SESSION_ID",
        "BRIDGE_SUB_SESSION_ID",
        "BRIDGE_WINDOW_ID",
        "BRIDGE_TEAM_ID",
        "BRIDGE_TASK_ID",
    ]
    old_rebound_env = {key: os.environ.get(key) for key in rebound_env_keys}
    try:
        os.environ["BRIDGE_RUNTIME_RUNS_ROOT"] = str(runs_root)
        os.environ["BRIDGE_SESSION_OBSERVER_ROOT"] = str(observer_root)
        session_binding = {
            "timestamp": _now(),
            "session_id": "subagent_session_demo",
            "run_id": "run_demo",
            "main_session_id": "main_demo",
            "sub_session_id": "sub_demo",
            "bridge_window_id": "bw_demo",
            "team_id": "team_demo",
            "task_id": "task_demo",
            "teammate_id": "executor",
            "agent_id": "executor",
            "agent_type": "teammate",
            "display_name": "executor",
            "session_kind": "bridge_child",
            "run_binding_state": "bound_to_run",
            "binding_source": "session_start",
        }
        module.append_jsonl(observer_root / "session_bindings.jsonl", session_binding)
        binding = module.observer_binding({"session_id": "subagent_session_demo"}, {"file_path": "README.md"})
        if binding.get("run_id") != "run_demo" or binding.get("bridge_window_id") != "bw_demo" or binding.get("teammate_id") != "executor":
            raise AssertionError(json.dumps(binding, ensure_ascii=False, indent=2))
        record = {
            "timestamp": _now(),
            **binding,
            "tool_name": "Read",
            "tool_use_id": "tool_subagent_read",
            "action": "read_file",
            "target": "README.md",
            "summary": "Read README.md",
            "status": "started",
            "started_at": _now(),
            "completed_at": None,
            "duration_ms": None,
            "normalized_input": {"file_path": "README.md"},
            "safe_input_preview": {"file_path": "README.md"},
            "file_refs": [{"path": "README.md", "role": "read"}],
            "output_summary": None,
        }
        module.emit_observer_record("tool_events", record)
        run_tool_events = _read_jsonl(runs_root / "run_demo" / "tool_events.jsonl")
        if not any(item.get("tool_name") == "Read" and item.get("teammate_id") == "executor" for item in run_tool_events):
            raise AssertionError(json.dumps(run_tool_events[-5:], ensure_ascii=False, indent=2))
        formal_command = "conda run -n mjy torchrun train.py --per_device_train_batch_size 1"
        formal_reminders = module.bash_execution_soft_reminders(
            "Bash",
            {"command": formal_command, "cwd": "."},
            binding,
            after=False,
        )
        reminder_codes = {item.get("code") for item in formal_reminders}
        if not {"executor_formal_gpu_probe_missing", "executor_log_manifest_reminder"}.issubset(reminder_codes):
            raise AssertionError(json.dumps(formal_reminders, ensure_ascii=False, indent=2))
        smoke_reminders = module.bash_execution_soft_reminders(
            "Bash",
            {"command": "conda run -n mjy python train.py --smoke --max_steps=1", "cwd": "."},
            binding,
            after=False,
        )
        smoke_codes = {item.get("code") for item in smoke_reminders}
        if "executor_formal_gpu_probe_missing" in smoke_codes or "executor_smoke_gpu_probe_recommended" not in smoke_codes:
            raise AssertionError(json.dumps(smoke_reminders, ensure_ascii=False, indent=2))
        bash_record = {
            "timestamp": _now(),
            **binding,
            "tool_name": "Bash",
            "tool_use_id": "tool_executor_train",
            "action": "run_command",
            "target": formal_command,
            "summary": "Bash formal train",
            "status": "started",
            "started_at": _now(),
            "completed_at": None,
            "duration_ms": None,
            "normalized_input": {"command": formal_command, "cwd": "."},
            "safe_input_preview": {"command": formal_command},
            "file_refs": [{"path": ".", "role": "cwd"}],
            "output_summary": None,
            "soft_reminders": formal_reminders,
        }
        module.emit_observer_record("tool_events", bash_record)
        run_tool_events = _read_jsonl(runs_root / "run_demo" / "tool_events.jsonl")
        bash_events = [item for item in run_tool_events if item.get("tool_use_id") == "tool_executor_train"]
        if not bash_events or "executor_formal_gpu_probe_missing" not in {item.get("code") for item in bash_events[-1].get("soft_reminders", [])}:
            raise AssertionError(json.dumps(run_tool_events[-5:], ensure_ascii=False, indent=2))

        os.environ["BRIDGE_CHILD_CLAUDE_SESSION"] = "1"
        os.environ["BRIDGE_RUN_ID"] = "run_demo"
        os.environ["CLAUDE_CONTROL_RUN_ID"] = "run_demo"
        os.environ["BRIDGE_MAIN_SESSION_ID"] = "main_demo"
        os.environ["CLAUDE_CONTROL_MAIN_SESSION_ID"] = "main_demo"
        os.environ["BRIDGE_SUB_SESSION_ID"] = "sub_anomaly"
        os.environ["BRIDGE_WINDOW_ID"] = "bw_anomaly"
        os.environ["BRIDGE_TEAM_ID"] = "team_anomaly"
        os.environ["BRIDGE_TASK_ID"] = "task_anomaly"
        anomaly_binding = module.observer_binding(
            {"session_id": "anomaly_session_demo", "agent_type": "anomaly-analyst-a"},
            {"file_path": "logs/anomaly.log"},
        )
        expected_anomaly = {
            "run_id": "run_demo",
            "main_session_id": "main_demo",
            "sub_session_id": "sub_anomaly",
            "bridge_window_id": "bw_anomaly",
            "team_id": "team_anomaly",
            "task_id": "task_anomaly",
            "teammate_id": "anomaly-analyst-a",
            "agent_type": "anomaly-analyst-a",
        }
        for key, expected in expected_anomaly.items():
            if anomaly_binding.get(key) != expected:
                raise AssertionError(json.dumps({"key": key, "binding": anomaly_binding}, ensure_ascii=False, indent=2))
        module.emit_observer_record("session_bindings", {"timestamp": _now(), **anomaly_binding})
        for tool_name, tool_input in [
            ("Read", {"file_path": "logs/anomaly.log"}),
            ("Grep", {"pattern": "error|oom", "path": "logs"}),
            ("Bash", {"command": "python - <<'PY'\nprint('inspect anomaly')\nPY", "cwd": "."}),
        ]:
            tool_use_id = f"tool_anomaly_{tool_name.lower()}"
            for status in ["started", "completed"]:
                module.emit_observer_record(
                    "tool_events",
                    {
                        "timestamp": _now(),
                        **anomaly_binding,
                        "tool_name": tool_name,
                        "tool_use_id": tool_use_id,
                        "action": "run_command" if tool_name == "Bash" else ("search" if tool_name == "Grep" else "read_file"),
                        "target": tool_input.get("file_path") or tool_input.get("path") or tool_input.get("command"),
                        "summary": f"{tool_name} anomaly evidence",
                        "status": status,
                        "started_at": _now(),
                        "completed_at": _now() if status == "completed" else None,
                        "duration_ms": 1 if status == "completed" else None,
                        "normalized_input": tool_input,
                        "safe_input_preview": module.safe_input_preview(tool_input),
                        "file_refs": module.tool_file_refs(tool_name, tool_input, after=status == "completed"),
                        "output_summary": None,
                    },
                )
        run_bindings = _read_jsonl(runs_root / "run_demo" / "session_bindings.jsonl")
        if not any(item.get("session_id") == "anomaly_session_demo" and item.get("teammate_id") == "anomaly-analyst-a" for item in run_bindings):
            raise AssertionError(json.dumps(run_bindings[-10:], ensure_ascii=False, indent=2))
        run_tool_events = _read_jsonl(runs_root / "run_demo" / "tool_events.jsonl")
        for tool_name in ["Read", "Grep", "Bash"]:
            statuses = {
                item.get("status")
                for item in run_tool_events
                if item.get("teammate_id") == "anomaly-analyst-a" and item.get("tool_name") == tool_name
            }
            if not {"started", "completed"}.issubset(statuses):
                raise AssertionError(json.dumps({"tool_name": tool_name, "statuses": sorted(statuses), "tail": run_tool_events[-20:]}, ensure_ascii=False, indent=2))

        os.environ["BRIDGE_SUB_SESSION_ID"] = "sub_chiefmate"
        os.environ["BRIDGE_WINDOW_ID"] = "bw_chiefmate"
        os.environ["BRIDGE_TEAM_ID"] = "team_chiefmate"
        os.environ["BRIDGE_TASK_ID"] = "task_chiefmate"
        chiefmate_binding = module.observer_binding(
            {"session_id": "chiefmate_session_demo", "agent_name": "chiefmate-a", "hook_event_name": "SubagentStart"},
            {},
        )
        expected_chiefmate = {
            "run_id": "run_demo",
            "main_session_id": "main_demo",
            "sub_session_id": "sub_chiefmate",
            "bridge_window_id": "bw_chiefmate",
            "team_id": "team_chiefmate",
            "task_id": "task_chiefmate",
            "teammate_id": "chiefmate-a",
            "agent_type": "chiefmate-a",
            "session_id": "chiefmate_session_demo",
        }
        for key, expected in expected_chiefmate.items():
            if chiefmate_binding.get(key) != expected:
                raise AssertionError(json.dumps({"key": key, "binding": chiefmate_binding}, ensure_ascii=False, indent=2))
        module.emit_observer_record("session_bindings", {"timestamp": _now(), **chiefmate_binding})
        for key in ["BRIDGE_RUN_ID", "CLAUDE_CONTROL_RUN_ID", *rebound_env_keys]:
            os.environ.pop(key, None)
        rebound_binding = module.observer_binding({"session_id": "chiefmate_session_demo"}, {"path": "src"})
        for key, expected in expected_chiefmate.items():
            if rebound_binding.get(key) != expected:
                raise AssertionError(json.dumps({"key": key, "binding": rebound_binding}, ensure_ascii=False, indent=2))
        required_tool_fields = {
            "run_id",
            "bridge_window_id",
            "team_id",
            "task_id",
            "teammate_id",
            "agent_type",
            "session_id",
            "tool_name",
            "status",
            "timestamp",
        }
        for tool_name, tool_input in [
            ("Read", {"file_path": "README.md"}),
            ("Grep", {"pattern": "TODO", "path": "."}),
            ("Glob", {"pattern": "*.md", "path": "."}),
            ("LS", {"path": "."}),
            ("Bash", {"command": "echo inspect", "cwd": "."}),
        ]:
            tool_use_id = f"tool_chiefmate_{tool_name.lower()}"
            for status in ["started", "completed"]:
                tool_binding = module.observer_binding({"session_id": "chiefmate_session_demo"}, tool_input)
                module.emit_observer_record(
                    "tool_events",
                    {
                        "timestamp": _now(),
                        **tool_binding,
                        "tool_name": tool_name,
                        "tool_use_id": tool_use_id,
                        "action": "run_command" if tool_name == "Bash" else "inspect",
                        "target": tool_input.get("file_path") or tool_input.get("path") or tool_input.get("command"),
                        "summary": f"{tool_name} chiefmate evidence",
                        "status": status,
                        "started_at": _now(),
                        "completed_at": _now() if status == "completed" else None,
                        "duration_ms": 1 if status == "completed" else None,
                        "normalized_input": tool_input,
                        "safe_input_preview": module.safe_input_preview(tool_input),
                        "file_refs": module.tool_file_refs(tool_name, tool_input, after=status == "completed"),
                        "output_summary": None,
                    },
                )
        run_bindings = _read_jsonl(runs_root / "run_demo" / "session_bindings.jsonl")
        if not any(item.get("session_id") == "chiefmate_session_demo" and item.get("teammate_id") == "chiefmate-a" for item in run_bindings):
            raise AssertionError(json.dumps(run_bindings[-10:], ensure_ascii=False, indent=2))
        run_tool_events = _read_jsonl(runs_root / "run_demo" / "tool_events.jsonl")
        for tool_name in ["Read", "Grep", "Glob", "LS", "Bash"]:
            matching = [
                item
                for item in run_tool_events
                if item.get("teammate_id") == "chiefmate-a"
                and item.get("session_id") == "chiefmate_session_demo"
                and item.get("tool_name") == tool_name
            ]
            statuses = {item.get("status") for item in matching}
            if not {"started", "completed"}.issubset(statuses):
                raise AssertionError(json.dumps({"tool_name": tool_name, "statuses": sorted(statuses), "tail": run_tool_events[-20:]}, ensure_ascii=False, indent=2))
            for item in matching:
                missing = sorted(field for field in required_tool_fields if item.get(field) in {None, ""})
                if missing:
                    raise AssertionError(json.dumps({"missing": missing, "record": item}, ensure_ascii=False, indent=2))
    finally:
        if old_runs_root is None:
            os.environ.pop("BRIDGE_RUNTIME_RUNS_ROOT", None)
        else:
            os.environ["BRIDGE_RUNTIME_RUNS_ROOT"] = old_runs_root
        if old_observer_root is None:
            os.environ.pop("BRIDGE_SESSION_OBSERVER_ROOT", None)
        else:
            os.environ["BRIDGE_SESSION_OBSERVER_ROOT"] = old_observer_root
        if old_run_id is not None:
            os.environ["BRIDGE_RUN_ID"] = old_run_id
        else:
            os.environ.pop("BRIDGE_RUN_ID", None)
        if old_control_run_id is not None:
            os.environ["CLAUDE_CONTROL_RUN_ID"] = old_control_run_id
        else:
            os.environ.pop("CLAUDE_CONTROL_RUN_ID", None)
        if old_child is not None:
            os.environ["BRIDGE_CHILD_CLAUDE_SESSION"] = old_child
        else:
            os.environ.pop("BRIDGE_CHILD_CLAUDE_SESSION", None)
        for key, old_value in old_rebound_env.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value
    return {"hook_observer_rebind": "passed"}


def run_state_graph_tests(control_root: Path, runs_root: Path) -> dict:
    real_control_root = Path(__file__).resolve().parents[1]
    real_validation = validate_state_graph(real_control_root)
    if not real_validation.get("valid"):
        raise AssertionError(json.dumps(real_validation, ensure_ascii=False, indent=2))
    fixture_validation = validate_state_graph(control_root)
    if not fixture_validation.get("valid"):
        raise AssertionError(json.dumps(fixture_validation, ensure_ascii=False, indent=2))
    state = replay_run_state(control_root, "run_demo", runtime_runs_root=str(runs_root))
    if state.get("lifecycle_state") != "bridge_window_returned":
        raise AssertionError(json.dumps(state, ensure_ascii=False, indent=2))
    if state.get("graph_node") != "bridge_window_returned":
        raise AssertionError(json.dumps(state, ensure_ascii=False, indent=2))
    graph = load_state_graph(control_root)
    rejected_state = graph.replay_events(
        control_root,
        [
            event("bridge_call_intended", "bw_reject_path", "sub_reject_path"),
            event("pretooluse_allowed_by_main_leader", "bw_reject_path", "sub_reject_path"),
            event("call_bridge_sdk_started", "bw_reject_path", "sub_reject_path"),
            event("bridge_window_opened", "bw_reject_path", "sub_reject_path"),
            event("bridge_packet_rejected", "bw_reject_path", "sub_reject_path"),
            event("bridge_result_returned", "bw_reject_path", "sub_reject_path"),
        ],
        runtime_runs_root=str(runs_root),
        run_id="run_demo",
    )
    if rejected_state.get("graph_node") != "bridge_window_returned":
        raise AssertionError(json.dumps(rejected_state, ensure_ascii=False, indent=2))
    failed_state = graph.replay_events(
        control_root,
        [
            event("bridge_call_intended", "bw_failed_path", "sub_failed_path"),
            event("pretooluse_allowed_by_main_leader", "bw_failed_path", "sub_failed_path"),
            event("call_bridge_sdk_error", "bw_failed_path", "sub_failed_path"),
        ],
        runtime_runs_root=str(runs_root),
        run_id="run_demo",
    )
    if failed_state.get("graph_node") not in {"terminal_failure", "bridge_call_failed", "bridge_window_failed"}:
        raise AssertionError(json.dumps(failed_state, ensure_ascii=False, indent=2))
    interrupted_state = graph.replay_events(
        control_root,
        [
            event("bridge_call_intended", "bw_interrupted_path", "sub_interrupted_path"),
            event("pretooluse_allowed_by_main_leader", "bw_interrupted_path", "sub_interrupted_path"),
            event("call_bridge_sdk_started", "bw_interrupted_path", "sub_interrupted_path"),
            event("bridge_call_interrupted", "bw_interrupted_path", "sub_interrupted_path"),
        ],
        runtime_runs_root=str(runs_root),
        run_id="run_demo",
    )
    if interrupted_state.get("graph_node") not in {"terminal_failure", "bridge_window_interrupted"}:
        raise AssertionError(json.dumps(interrupted_state, ensure_ascii=False, indent=2))
    mermaid = graph.export_mermaid()
    dot = graph.export_dot()
    if "flowchart TD" not in mermaid or "digraph RunBridgeStateGraph" not in dot:
        raise AssertionError(json.dumps({"mermaid": mermaid[:200], "dot": dot[:200]}, ensure_ascii=False, indent=2))
    (runs_root / "run_demo" / "state_graph.mmd").write_text(mermaid, encoding="utf-8")
    (runs_root / "run_demo" / "state_graph.dot").write_text(dot, encoding="utf-8")
    return {"state_graph": "passed", "node_count": real_validation["node_count"], "edge_count": real_validation["edge_count"]}


def run_retry_policy_tests(control_root: Path, runs_root: Path) -> dict:
    policies = load_retry_policies(control_root)
    p = packet("bw_success", "sub_success")
    decision = decide_retry(
        policies,
        "bridge_sdk_call",
        attempt=2,
        error_type="ClaudeCliTimeout",
        reason={"error_type": "ClaudeCliTimeout"},
    )
    event_payload = decision.as_event_payload(
        repo_key="unscoped_repo",
        run_id="run_demo",
        bridge_window_id="bw_success",
        packet_hash=packet_hash(p),
    )
    if not decision.retryable or decision.delay_ms != 4000 or event_payload["attempt"] != 2:
        raise AssertionError(json.dumps(event_payload, ensure_ascii=False, indent=2))
    non_retryable = decide_retry(
        policies,
        "bridge_sdk_call",
        attempt=1,
        error_type="FrozenSemanticsMismatch",
    )
    if non_retryable.retryable or non_retryable.next_action != "surface_non_retryable_failure":
        raise AssertionError(json.dumps(non_retryable.as_event_payload(repo_key="unscoped_repo", run_id="run_demo", bridge_window_id="bw_success", packet_hash=packet_hash(p)), ensure_ascii=False, indent=2))
    retry_event = event(
        "retry_attempt_scheduled",
        "bw_success",
        "sub_success",
        team_id="team_success",
        task_id="task_success",
        agent_type="runtime",
        agent_id="runtime.retry",
        payload=event_payload,
    )
    result = dispatch_workflow_event(str(control_root), retry_event, runtime_runs_root=str(runs_root), persist=True)
    if not result.ok:
        raise AssertionError(json.dumps(result.check_result, ensure_ascii=False, indent=2))
    retry_ledger = json.loads((runs_root / "run_demo" / "run_ledger.json").read_text(encoding="utf-8")).get("retry_context", {})
    if retry_ledger.get("latest", {}).get("attempt") != 2:
        raise AssertionError(json.dumps(retry_ledger, ensure_ascii=False, indent=2))
    bw = "bw_auto_retry"
    ss = "sub_auto_retry"
    auto_packet = packet(bw, ss)
    dispatch(control_root, runs_root, event("bridge_call_intended", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_auto_retry", payload={"packet": auto_packet}))
    dispatch(control_root, runs_root, event("pretooluse_allowed_by_main_leader", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_auto_retry", payload={"packet": auto_packet}))
    auto_result = dispatch_workflow_event(
        str(control_root),
        event("call_bridge_sdk_error", bw, ss, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_auto_retry", payload={"packet": auto_packet, "error_or_null": {"type": "ClaudeCliTimeout", "message": "timeout"}}),
        runtime_runs_root=str(runs_root),
        persist=True,
    )
    auto_plan = auto_result.check_result.get("derived_facts", {}).get("auto_recovery", {})
    if auto_plan.get("dispatch_event_kind") != "retry_attempt_scheduled":
        raise AssertionError(json.dumps(auto_result.check_result, ensure_ascii=False, indent=2))
    retry_events = [
        item for item in _read_jsonl(runs_root / "run_demo" / "event_log.jsonl")
        if item.get("event_kind") == "retry_attempt_scheduled" and item.get("bridge_window_id") == bw
    ]
    if not retry_events or retry_events[-1].get("payload", {}).get("attempt") != 2:
        raise AssertionError(json.dumps(retry_events, ensure_ascii=False, indent=2))
    auto_retry_payload = retry_events[-1].get("payload", {})
    if auto_retry_payload.get("retry_action", {}).get("kind") != "retry_bridge_sdk_call":
        raise AssertionError(json.dumps(auto_retry_payload, ensure_ascii=False, indent=2))
    if auto_retry_payload.get("retry_action", {}).get("requires_same_packet") is not True:
        raise AssertionError(json.dumps(auto_retry_payload, ensure_ascii=False, indent=2))

    bw_repair = "bw_completion_repair"
    ss_repair = "sub_completion_repair"
    repair_packet = packet(bw_repair, ss_repair)
    dispatch(control_root, runs_root, event("bridge_call_intended", bw_repair, ss_repair, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_completion_repair", payload={"packet": repair_packet}))
    dispatch(control_root, runs_root, event("pretooluse_allowed_by_main_leader", bw_repair, ss_repair, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_completion_repair", payload={"packet": repair_packet}))
    dispatch(control_root, runs_root, event("call_bridge_sdk_started", bw_repair, ss_repair, agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", tool_use_id="tool_completion_repair", payload={"packet": repair_packet}))
    dispatch(control_root, runs_root, event("bridge_window_opened", bw_repair, ss_repair, agent_type="bridge-leader", payload={"packet": repair_packet}))
    dispatch(control_root, runs_root, event("bridge_packet_accepted", bw_repair, ss_repair, payload={"packet": repair_packet}))
    dispatch(control_root, runs_root, event("team_create_started", bw_repair, ss_repair, team_id="team_completion_repair", tool_name="team_create"))
    dispatch(control_root, runs_root, event("team_create_succeeded", bw_repair, ss_repair, team_id="team_completion_repair", tool_name="team_create"))
    dispatch(control_root, runs_root, event("task_create_started", bw_repair, ss_repair, team_id="team_completion_repair", task_id="task_completion_repair", tool_name="task_create"))
    dispatch(control_root, runs_root, event("task_create_succeeded", bw_repair, ss_repair, team_id="team_completion_repair", task_id="task_completion_repair", tool_name="task_create"))
    dispatch(
        control_root,
        runs_root,
        event(
            "taskcreated_hook_accepted",
            bw_repair,
            ss_repair,
            team_id="team_completion_repair",
            task_id="task_completion_repair",
            agent_type="hook",
            agent_id="hook.task_created",
            payload={
                "task_subject": "task_completion_repair",
                "task_description": "completion repair task",
                "task_spec": repair_packet["task_spec"],
                "team_spec": repair_packet["team_spec"],
                "task_team_mapping": repair_packet["task_team_mapping"],
            },
        ),
    )
    dispatch(control_root, runs_root, event("message_dispatch_started", bw_repair, ss_repair, team_id="team_completion_repair", task_id="task_completion_repair", tool_name="send_messages"))
    dispatch(control_root, runs_root, event("message_dispatch_succeeded", bw_repair, ss_repair, team_id="team_completion_repair", task_id="task_completion_repair", tool_name="send_messages"))
    dispatch(control_root, runs_root, event("artifacts_ready", bw_repair, ss_repair, team_id="team_completion_repair", task_id="task_completion_repair", tool_name="task_complete", payload={"artifact_refs": []}))
    rejected_once = dispatch_workflow_event(
        str(control_root),
        event(
            "completion_contract_rejected",
            bw_repair,
            ss_repair,
            team_id="team_completion_repair",
            task_id="task_completion_repair",
            agent_type="hook",
            agent_id="hook.task_completed",
            payload={"completion_contract": repair_packet["completion_contract"], "completion_checks": {"required_outputs_present": True, "required_artifacts_present": False, "validation_passed": False}, "missing_contract_items": ["log_manifest"]},
        ),
        runtime_runs_root=str(runs_root),
        persist=True,
    )
    repair_plan = rejected_once.check_result.get("derived_facts", {}).get("auto_recovery", {})
    if repair_plan.get("dispatch_event_kind") != "retry_attempt_scheduled" or repair_plan.get("retry_scope") != "completion_rejected":
        raise AssertionError(json.dumps(rejected_once.check_result, ensure_ascii=False, indent=2))
    repair_retry_events = [
        item for item in _read_jsonl(runs_root / "run_demo" / "event_log.jsonl")
        if item.get("event_kind") == "retry_attempt_scheduled" and item.get("bridge_window_id") == bw_repair
    ]
    repair_retry_payload = repair_retry_events[-1].get("payload", {}) if repair_retry_events else {}
    if repair_retry_payload.get("retry_action", {}).get("kind") != "repair_bridge_output":
        raise AssertionError(json.dumps(repair_retry_payload, ensure_ascii=False, indent=2))
    if repair_retry_payload.get("same_packet_boundary_required") is not True or repair_retry_payload.get("packet_hash") != packet_hash(repair_packet):
        raise AssertionError(json.dumps(repair_retry_payload, ensure_ascii=False, indent=2))
    driver_decision = evaluate_retry_attempt(control_root, repair_retry_events[-1], runtime_runs_root=str(runs_root))
    if not driver_decision.ready or not driver_decision.allowed or driver_decision.action_kind != "repair_bridge_output":
        raise AssertionError(json.dumps(driver_decision.as_dict(), ensure_ascii=False, indent=2))
    disabled_driver = dispatch_retry_action_stub(driver_decision)
    if disabled_driver.get("enabled") is not False:
        raise AssertionError(json.dumps(disabled_driver, ensure_ascii=False, indent=2))

    dispatch(control_root, runs_root, event("retry_artifact_collection", bw_repair, ss_repair, team_id="team_completion_repair", task_id="task_completion_repair", agent_type="bridge-leader", agent_id="bridge-leader"))
    rejected_twice = dispatch_workflow_event(
        str(control_root),
        event(
            "completion_contract_rejected",
            bw_repair,
            ss_repair,
            team_id="team_completion_repair",
            task_id="task_completion_repair",
            agent_type="hook",
            agent_id="hook.task_completed",
            payload={"completion_contract": repair_packet["completion_contract"], "completion_checks": {"required_outputs_present": True, "required_artifacts_present": False, "validation_passed": False}, "missing_contract_items": ["log_manifest"]},
        ),
        runtime_runs_root=str(runs_root),
        persist=True,
    )
    exhausted_plan = rejected_twice.check_result.get("derived_facts", {}).get("auto_recovery", {})
    if exhausted_plan.get("dispatch_event_kind") != "enter_anomaly" or exhausted_plan.get("retry_scope") != "completion_rejected":
        raise AssertionError(json.dumps(rejected_twice.check_result, ensure_ascii=False, indent=2))
    anomaly_events = [
        item for item in _read_jsonl(runs_root / "run_demo" / "event_log.jsonl")
        if item.get("event_kind") == "enter_anomaly" and item.get("bridge_window_id") == bw_repair
    ]
    if not anomaly_events:
        raise AssertionError(json.dumps(_read_jsonl(runs_root / "run_demo" / "event_log.jsonl")[-10:], ensure_ascii=False, indent=2))
    retry_ledger = json.loads((runs_root / "run_demo" / "run_ledger.json").read_text(encoding="utf-8")).get("retry_context", {})
    completion_attempts = [item for item in retry_ledger.get("attempts", []) if item.get("bridge_window_id") == bw_repair and item.get("retry_scope") == "completion_rejected"]
    if len(completion_attempts) != 1 or completion_attempts[0].get("attempt") != 2:
        raise AssertionError(json.dumps(retry_ledger, ensure_ascii=False, indent=2))
    scheduled = load_scheduled_retry_events(control_root, "run_demo", runtime_runs_root=str(runs_root))
    if not any(item.get("bridge_window_id") == bw_repair for item in scheduled):
        raise AssertionError(json.dumps(scheduled[-5:], ensure_ascii=False, indent=2))
    return {"retry_policy": "passed"}


def run_guardrail_tests(control_root: Path, runs_root: Path) -> dict:
    invalid = validate_bridge_result({"status": "succeeded", "reports": [], "artifact_refs": [], "cleanup_required": False})
    if invalid.get("valid") or invalid.get("error_type") not in {"SchemaValidationFailed", "MissingReport"}:
        raise AssertionError(json.dumps(invalid, ensure_ascii=False, indent=2))
    strict_ok = validate_teammate_report(report(), strict=True)
    if not strict_ok.get("valid"):
        raise AssertionError(json.dumps(strict_ok, ensure_ascii=False, indent=2))
    strict_bad = validate_teammate_report({"summary": "bad", "instruction_coverage": {"completed": "item"}}, strict=True)
    if strict_bad.get("valid") or strict_bad.get("error_type") != "InvalidCoverageDisposition":
        raise AssertionError(json.dumps(strict_bad, ensure_ascii=False, indent=2))
    summary_only = validate_bridge_result({"status": "succeeded", "reports": [{"summary": "only"}], "artifact_refs": ["artifact"], "evidence": {"event_ids": ["evt"]}, "error_or_null": None, "cleanup_required": False})
    if summary_only.get("valid") or summary_only.get("error_type") not in {"SchemaValidationFailed", "MissingInstructionCoverage"}:
        raise AssertionError(json.dumps(summary_only, ensure_ascii=False, indent=2))
    completed_without_evidence = validate_teammate_report({"summary": "bad", "instruction_coverage": {"item": "completed"}}, strict=True)
    if completed_without_evidence.get("valid") or completed_without_evidence.get("error_type") != "MissingRequiredEvidenceRef":
        raise AssertionError(json.dumps(completed_without_evidence, ensure_ascii=False, indent=2))
    completion_invalid = validate_completion_report({"completion_checks": {"required_outputs_present": True}, "artifact_refs": []})
    if completion_invalid.get("valid"):
        raise AssertionError(json.dumps(completion_invalid, ensure_ascii=False, indent=2))
    completion_without_evidence = validate_completion_report(
        {
            "completion_checks": {"required_outputs_present": False, "required_artifacts_present": False, "validation_passed": True},
            "reports": [],
            "artifact_refs": [],
        }
    )
    if completion_without_evidence.get("valid") or completion_without_evidence.get("error_type") != "MissingRequiredEvidenceRef":
        raise AssertionError(json.dumps(completion_without_evidence, ensure_ascii=False, indent=2))
    artifact_claim_without_refs = validate_completion_report(
        {
            "completion_checks": {"required_outputs_present": True, "required_artifacts_present": True, "validation_passed": False},
            "reports": [report()],
            "artifact_refs": [],
        }
    )
    if artifact_claim_without_refs.get("valid") or artifact_claim_without_refs.get("error_type") != "MissingArtifactRefs":
        raise AssertionError(json.dumps(artifact_claim_without_refs, ensure_ascii=False, indent=2))
    bad_manifest = validate_log_manifest({"run_id": "run_demo", "bridge_window_id": "bw", "task_id": "task", "terminal_status": "completed"})
    if bad_manifest.get("valid") or bad_manifest.get("error_type") != "SchemaValidationFailed":
        raise AssertionError(json.dumps(bad_manifest, ensure_ascii=False, indent=2))
    formal_missing_memory = validate_log_manifest(
        {
            "run_id": "run_demo",
            "bridge_window_id": "bw",
            "task_id": "task",
            "command": "python train.py --formal",
            "cwd": ".",
            "environment_evidence": {"conda_env": "mjy"},
            "process_refs": [{"pid": 123}],
            "terminal_status": "completed",
            "batchbasis": {"per_device_train_batch_size": 1},
            "gpu_id_or_device_ids": ["0"],
            "model_or_model_family": "demo",
            "dataset_name_split_source": "demo",
            "method_or_objective": "demo",
        },
        formal_run=True,
    )
    if formal_missing_memory.get("valid") or formal_missing_memory.get("error_type") != "MissingFormalRunEvidence":
        raise AssertionError(json.dumps(formal_missing_memory, ensure_ascii=False, indent=2))
    bad_event = event(
        "bridge_result_returned",
        "bw_guardrail",
        "sub_guardrail",
        agent_type="main-leader",
        agent_id="main",
        tool_name="call_bridge_sdk",
        payload={"bridge_result": {"status": "succeeded", "reports": [], "artifact_refs": [], "cleanup_required": False}},
    )
    result = dispatch_workflow_event(str(control_root), bad_event, runtime_runs_root=str(runs_root), persist=True)
    if result.ok or "bridge_result_guardrail_failed" not in result.check_result.get("reasons", []):
        raise AssertionError(json.dumps(result.check_result, ensure_ascii=False, indent=2))
    auto_repair = result.check_result.get("derived_facts", {}).get("auto_recovery", {})
    if auto_repair.get("dispatch_event_kind") != "retry_attempt_scheduled" or auto_repair.get("retry_scope") != "completion_rejected":
        raise AssertionError(json.dumps(result.check_result, ensure_ascii=False, indent=2))
    trajectory = (runs_root / "run_demo" / "trajectory.jsonl").read_text(encoding="utf-8")
    if "guardrail_validation" not in trajectory and "SchemaValidationFailed" not in trajectory and "MissingReport" not in trajectory:
        raise AssertionError(trajectory[-1000:])
    return {"guardrails": "passed"}


def run_checkpoint_trajectory_tests(runs_root: Path) -> dict:
    run_root = runs_root / "run_demo"
    required = [
        run_root / "checkpoints.jsonl",
        run_root / "latest_checkpoint.json",
        run_root / "trajectory.jsonl",
        run_root / "trajectory_index.json",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise AssertionError(json.dumps({"missing": missing}, ensure_ascii=False, indent=2))
    trajectory_text = (run_root / "trajectory.jsonl").read_text(encoding="utf-8")
    if "sk-" in trajectory_text or "api_key=" in trajectory_text.lower():
        raise AssertionError("trajectory contains unredacted secret-looking text")
    index = json.loads((run_root / "trajectory_index.json").read_text(encoding="utf-8"))
    if int(index.get("step_count") or 0) <= 0:
        raise AssertionError(json.dumps(index, ensure_ascii=False, indent=2))
    latest_checkpoint = json.loads((run_root / "latest_checkpoint.json").read_text(encoding="utf-8"))
    graph_node = (latest_checkpoint.get("state") or {}).get("graph_node") if isinstance(latest_checkpoint.get("state"), dict) else None
    if graph_node in {None, "read_runtime_truth"}:
        raise AssertionError(json.dumps(latest_checkpoint, ensure_ascii=False, indent=2))
    if not isinstance(index.get("completion_checks"), list):
        raise AssertionError(json.dumps(index, ensure_ascii=False, indent=2))
    first_step = json.loads(trajectory_text.splitlines()[0])
    for field in ["supports_refs", "produces_refs", "related_completion_check_refs", "related_artifact_refs"]:
        if field not in first_step:
            raise AssertionError(json.dumps(first_step, ensure_ascii=False, indent=2))
    return {"checkpoint_trajectory": "passed", "trajectory_steps": index["step_count"]}


def run_checkpoint_write_order_test(control_root: Path, runs_root: Path) -> dict:
    bw = "bw_checkpoint_order"
    ss = "sub_checkpoint_order"
    p = packet(bw, ss)
    payload = event(
        "bridge_call_intended",
        bw,
        ss,
        agent_type="main-leader",
        agent_id="main",
        tool_name="call_bridge_sdk",
        tool_use_id="tool_checkpoint_order",
        payload={"packet": p},
    )
    payload["event_id"] = "evt_checkpoint_order"
    dispatch(control_root, runs_root, payload)
    latest_checkpoint = json.loads((runs_root / "run_demo" / "latest_checkpoint.json").read_text(encoding="utf-8"))
    state = latest_checkpoint.get("state") if isinstance(latest_checkpoint.get("state"), dict) else {}
    if latest_checkpoint.get("event_id") != "evt_checkpoint_order":
        raise AssertionError(json.dumps(latest_checkpoint, ensure_ascii=False, indent=2))
    if state.get("graph_node") != "bridge_call_intended":
        raise AssertionError(json.dumps(latest_checkpoint, ensure_ascii=False, indent=2))
    if state.get("lifecycle_state") != "bridge_call_intended":
        raise AssertionError(json.dumps(latest_checkpoint, ensure_ascii=False, indent=2))
    return {"checkpoint_write_order": "passed"}


def run_multi_repo_isolation_tests(root: Path, control_root: Path) -> dict:
    repo_a = root / "repo_a"
    repo_b = root / "repo_b"
    repo_a.mkdir(parents=True, exist_ok=True)
    repo_b.mkdir(parents=True, exist_ok=True)
    key_a = resolve_repo_key(repo_a)
    key_b = resolve_repo_key(repo_b)
    if key_a == key_b:
        raise AssertionError("repo keys collided")
    ensure_repo_registered(control_root, repo_a, run_id="run_same", status="running")
    ensure_repo_registered(control_root, repo_b, run_id="run_same", status="running")
    runs_a = get_repo_runtime_root(control_root, key_a)
    runs_b = get_repo_runtime_root(control_root, key_b)
    for repo_key, repo_path, runs_root in ((key_a, repo_a, runs_a), (key_b, repo_b, runs_b)):
        payload = {
            "run_id": "run_same",
            "main_session_id": f"main_{repo_path.name}",
            "agent_id": "hook.session_start",
            "agent_type": "hook",
            "event_kind": "session_started",
            "timestamp": _now(),
            "payload": {"cwd": str(repo_path), "repo_root": str(repo_path), "repo_key": repo_key},
        }
        result = dispatch_workflow_event(str(control_root), payload, runtime_runs_root=str(runs_root), persist=True)
        if not result.ok:
            raise AssertionError(json.dumps(result.check_result, ensure_ascii=False, indent=2))
        if result.runtime_snapshot.get("repo_key") != repo_key:
            raise AssertionError(json.dumps(result.runtime_snapshot, ensure_ascii=False, indent=2))
    if not (runs_a / "run_same" / "run_ledger.json").exists() or not (runs_b / "run_same" / "run_ledger.json").exists():
        raise AssertionError("isolated run ledgers were not written")
    if runs_a == runs_b:
        raise AssertionError("repo runtime roots are not isolated")
    explicit = dispatch_workflow_event(
        str(control_root),
        {
            "run_id": "run_explicit_repo_key",
            "main_session_id": "main_explicit_repo_key",
            "agent_id": "hook.session_start",
            "agent_type": "hook",
            "event_kind": "session_started",
            "timestamp": _now(),
            "payload": {"repo_root": str(repo_a)},
        },
        repo_key=key_a,
        persist=True,
    )
    if explicit.runtime_snapshot.get("repo_key") != key_a or not (runs_a / "run_explicit_repo_key" / "event_log.jsonl").exists():
        raise AssertionError(json.dumps(explicit.runtime_snapshot, ensure_ascii=False, indent=2))
    bridge_server_path = Path(__file__).resolve().parents[1] / "mcp" / "bridge_server.py"
    spec = importlib.util.spec_from_file_location("bridge_server_smoke", bridge_server_path)
    if spec is None or spec.loader is None:
        raise AssertionError(str(bridge_server_path))
    bridge_server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge_server)
    call_schema = next(item for item in bridge_server._tools() if item.get("name") == "call_bridge_sdk")["inputSchema"]
    if "repo_key" not in call_schema.get("properties", {}):
        raise AssertionError(json.dumps(call_schema, ensure_ascii=False, indent=2))
    packet_a = packet("bw_repo_a_last", "sub_repo_a_last")
    packet_a["binding"]["repo_key"] = key_a
    packet_b = packet("bw_repo_b_last", "sub_repo_b_last")
    packet_b["binding"]["repo_key"] = key_b
    bridge_server._save_last_packet(runs_a, packet_a)
    bridge_server._save_last_packet(runs_b, packet_b)
    if bridge_server._load_last_packet(runs_a).get("binding", {}).get("repo_key") != key_a:
        raise AssertionError(json.dumps(bridge_server._load_last_packet(runs_a), ensure_ascii=False, indent=2))
    if bridge_server._load_last_packet(runs_b).get("binding", {}).get("repo_key") != key_b:
        raise AssertionError(json.dumps(bridge_server._load_last_packet(runs_b), ensure_ascii=False, indent=2))
    if bridge_server._packet_repo_key(packet_a) != key_a or bridge_server._packet_repo_key(packet_b) != key_b:
        raise AssertionError(json.dumps({"packet_a": packet_a, "packet_b": packet_b}, ensure_ascii=False, indent=2))
    registered = {repo.repo_key for repo in list_registered_repos(control_root)}
    if key_a not in registered or key_b not in registered:
        raise AssertionError(json.dumps(sorted(registered), ensure_ascii=False, indent=2))
    return {"multi_repo_isolation": "passed", "repo_keys": [key_a, key_b]}


def run_bridge_boundary_tests(control_root: Path, runs_root: Path) -> dict:
    p = packet("bw_boundary_executor", "sub_boundary_executor")
    request = BridgeExecutionRequest.from_execution_input(
        {
            "packet": p,
            "run_id": "run_demo",
            "main_session_id": "main_demo",
            "sub_session_id": "sub_boundary_executor",
            "bridge_window_id": "bw_boundary_executor",
            "team_id": "team_boundary_executor",
            "task_id": "task_boundary_executor",
        }
    )
    simulated = SimulateBridgeExecutor().execute(request)
    if simulated.get("status") != "succeeded" or not simulated.get("reports"):
        raise AssertionError(json.dumps(simulated, ensure_ascii=False, indent=2))
    sdk = SdkBridgeExecutor().execute(request)
    if sdk.get("status") != "failed" or sdk.get("error_or_null", {}).get("type") != "SdkExecutorNotImplemented":
        raise AssertionError(json.dumps(sdk, ensure_ascii=False, indent=2))
    if CliBridgeExecutor().name != "cli":
        raise AssertionError("cli executor name mismatch")
    previous = os.environ.get("BRIDGE_EXECUTOR")
    try:
        os.environ["BRIDGE_EXECUTOR"] = "simulate"
        if bridge_executor_from_env().name != "simulate":
            raise AssertionError("BRIDGE_EXECUTOR=simulate did not select simulate")
        os.environ["BRIDGE_EXECUTOR"] = "sdk"
        if bridge_executor_from_env().name != "sdk":
            raise AssertionError("BRIDGE_EXECUTOR=sdk did not select sdk")
        os.environ["BRIDGE_EXECUTOR"] = "canary"
        if bridge_executor_from_env().name != "cli":
            raise AssertionError("BRIDGE_EXECUTOR=canary did not select cli fallback")
    finally:
        if previous is None:
            os.environ.pop("BRIDGE_EXECUTOR", None)
        else:
            os.environ["BRIDGE_EXECUTOR"] = previous
    return {"bridge_boundary": "passed"}


def run_event_artifact_completion_tests(control_root: Path, runs_root: Path) -> dict:
    p = packet("bw_event_artifact", "sub_event_artifact")
    context = {
        "run_id": "run_demo",
        "bridge_window_id": "bw_event_artifact",
        "team_id": "team_event_artifact",
        "task_id": "task_event_artifact",
        "agent_id": "bridge-leader",
        "event_id": "evt_event_artifact",
        "timestamp": _now(),
    }
    envelope = normalize_runtime_event(
        {
            "event_id": "evt_event_artifact",
            "run_id": "run_demo",
            "sub_session_id": "sub_event_artifact",
            "bridge_window_id": "bw_event_artifact",
            "team_id": "team_event_artifact",
            "task_id": "task_event_artifact",
            "event_kind": "runtime_transition",
            "payload": {"secret_token": "sk-should-not-leak-1234567890"},
        },
        source="runtime",
        authority="authoritative",
        seq=7,
        payload_ref="event_log.jsonl:evt_event_artifact",
    )
    if envelope.get("schema_version") != "runtime_event_envelope.v1" or envelope.get("authority") != "authoritative":
        raise AssertionError(json.dumps(envelope, ensure_ascii=False, indent=2))
    if "sk-should-not-leak" in envelope.get("safe_preview", ""):
        raise AssertionError(json.dumps(envelope, ensure_ascii=False, indent=2))
    observed = normalize_stream_record(
        {"event_id": "evt_cli_stream", "run_id": "run_demo", "event_kind": "assistant_delta", "sequence": 3},
        source="cli",
        authority="observed",
        payload_ref="sdk_stream_events.jsonl:3",
    )
    if observed.get("source") != "cli" or observed.get("seq") != 3 or observed.get("authority") != "observed":
        raise AssertionError(json.dumps(observed, ensure_ascii=False, indent=2))

    manifest_path = runs_root / "run_demo" / "manifests" / "event_artifact_log_manifest.json"
    _write_json(
        manifest_path,
        {
            "run_id": "run_demo",
            "bridge_window_id": "bw_event_artifact",
            "task_id": "task_event_artifact",
            "command": "conda run -n mjy python train.py",
            "cwd": ".",
            "batchbasis": "smoke selected batch 8",
            "gpu_id": "0",
            "memory": "warmup memory observed 72GB",
            "model": "demo model",
            "dataset": "demo dataset",
            "method": "demo method",
        },
    )
    refs = normalize_artifact_refs(
        [
            "artifact",
            {"ref_type": "log_manifest", "path": str(manifest_path), "producer": {"agent_id": "bridge-leader", "event_id": "evt_event_artifact"}},
        ],
        context=context,
        base_dir=runs_root,
    )
    valid_refs = validate_artifact_refs(refs, required_artifacts=["artifact", "log_manifest"], context=context, base_dir=runs_root)
    if not valid_refs.get("valid"):
        raise AssertionError(json.dumps(valid_refs, ensure_ascii=False, indent=2))
    generic_missing = validate_artifact_refs([refs[1]], required_artifacts=["artifact"], context=context, base_dir=runs_root)
    if generic_missing.get("valid"):
        raise AssertionError(json.dumps(generic_missing, ensure_ascii=False, indent=2))
    stale = dict(refs[1])
    stale["bridge_window_id"] = "bw_other_window"
    stale_validation = validate_artifact_refs([stale], required_artifacts=["log_manifest"], context=context, base_dir=runs_root)
    if stale_validation.get("valid") or not any(item.get("status") == "block" for item in stale_validation.get("checks", [])):
        raise AssertionError(json.dumps(stale_validation, ensure_ascii=False, indent=2))

    success_execution = {
        "status": "succeeded",
        "reports": [report_for_packet(p)],
        "artifact_refs": refs,
        "evidence": {"manifest_required_fields_checklist": _manifest_required_fields_checklist(p)},
        "error_or_null": None,
        "cleanup_required": False,
    }
    success_validation = validate_bridge_completion(p, success_execution, context=context, control_root=control_root, base_dir=runs_root)
    if not completion_succeeded(success_validation):
        raise AssertionError(json.dumps(success_validation, ensure_ascii=False, indent=2))

    missing_artifact = dict(success_execution)
    missing_artifact["artifact_refs"] = []
    missing_validation = validate_bridge_completion(p, missing_artifact, context=context, control_root=control_root, base_dir=runs_root)
    if completion_succeeded(missing_validation) or missing_validation.get("final_disposition") != "blocked":
        raise AssertionError(json.dumps(missing_validation, ensure_ascii=False, indent=2))

    running_execution = dict(success_execution)
    running_execution["waiting"] = True
    running_execution["owned_process_refs"] = [{"pid": 123, "status": "running"}]
    running_validation = validate_bridge_completion(p, running_execution, context=context, control_root=control_root, base_dir=runs_root)
    if completion_succeeded(running_validation) or running_validation.get("final_disposition") != "blocked":
        raise AssertionError(json.dumps(running_validation, ensure_ascii=False, indent=2))
    return {"event_artifact_completion": "passed"}


def run_policy_team_projection_tests(control_root: Path, runs_root: Path) -> dict:
    compiled = compile_policy(control_root)
    if "bridge_result.schema.json" not in compiled.schemas or "completion_report.schema.json" not in compiled.schemas:
        raise AssertionError(json.dumps(sorted(compiled.schemas.keys()), ensure_ascii=False, indent=2))
    if not compiled.phase_contracts.get("base_completion_contract"):
        raise AssertionError(json.dumps(compiled.phase_contracts, ensure_ascii=False, indent=2))
    if compiled.team_planner.get("enabled") is not True:
        raise AssertionError(json.dumps(compiled.team_planner, ensure_ascii=False, indent=2))
    invalid_policy = [item for item in compiled.validation_results if not item.get("valid")]
    if invalid_policy:
        raise AssertionError(json.dumps(invalid_policy, ensure_ascii=False, indent=2))

    decision = RiskBasedTeamSelector().select(
        target_phase="l3_bridge",
        task_spec={"task_subject": "read docs and report", "task_description": "bounded preflight", "task_kind": "preflight"},
        policy_teammates=[
            {"teammate_name": "preflight-initial", "role": "preflight"},
            {"teammate_name": "curator", "role": "curator"},
            {"teammate_name": "refresher", "role": "documentation"},
        ],
    )
    if decision.reason != "risk_reduced_team" or [item.get("teammate_name") for item in decision.selected_teammates] != ["preflight-initial"]:
        raise AssertionError(json.dumps({"reason": decision.reason, "selected": decision.selected_teammates}, ensure_ascii=False, indent=2))

    low_risk_l3 = decide_next_bridge_packet(
        str(control_root),
        "run_demo",
        runtime_runs_root=str(runs_root),
        main_session_id="main_demo",
        user_instruction="read repository docs and report current status",
        task_spec={"task_subject": "read docs status", "task_kind": "preflight"},
        target_phase="l3_bridge",
    )
    low_names = [item.get("teammate_name") for item in low_risk_l3.get("team_spec", {}).get("teammate_specs", [])]
    if low_names != ["preflight-initial"] or low_risk_l3.get("team_planning", {}).get("reason") != "risk_reduced_team":
        raise AssertionError(json.dumps(low_risk_l3.get("team_planning"), ensure_ascii=False, indent=2))

    write_risk_l3 = decide_next_bridge_packet(
        str(control_root),
        "run_demo",
        runtime_runs_root=str(runs_root),
        main_session_id="main_demo",
        user_instruction="update CLAUDE.md and README with current workflow behavior",
        task_spec={"task_subject": "documentation update", "task_kind": "documentation"},
        target_phase="l3_bridge",
    )
    write_names = [item.get("teammate_name") for item in write_risk_l3.get("team_spec", {}).get("teammate_specs", [])]
    if "curator" not in write_names or write_risk_l3.get("team_planning", {}).get("reason") == "risk_reduced_team":
        raise AssertionError(json.dumps(write_risk_l3.get("team_planning"), ensure_ascii=False, indent=2))

    snapshot = json.loads((runs_root / "run_demo" / "runtime_snapshot.json").read_text(encoding="utf-8"))
    if not snapshot.get("snapshot_refs", {}).get("canonical_event_log"):
        raise AssertionError(json.dumps(snapshot.get("snapshot_refs"), ensure_ascii=False, indent=2))
    last_result = snapshot.get("last_bridge_result") if isinstance(snapshot.get("last_bridge_result"), dict) else {}
    if last_result.get("detail_level") != "compact" or "reports" in last_result:
        raise AssertionError(json.dumps(last_result, ensure_ascii=False, indent=2))
    companion_events = _read_jsonl(runs_root / "run_demo" / "companion_events.jsonl")
    if companion_events and any((item.get("runtime_event") or {}).get("authority") == "authoritative" for item in companion_events):
        raise AssertionError(json.dumps(companion_events[:5], ensure_ascii=False, indent=2))
    completion_events = _read_jsonl(runs_root / "run_demo" / "completion_checks.jsonl")
    if not any((item.get("completion_checks") or {}).get("validated_by") == "completion_validator.v1" for item in completion_events):
        raise AssertionError(json.dumps(completion_events[-5:], ensure_ascii=False, indent=2))
    run_ledger = json.loads((runs_root / "run_demo" / "run_ledger.json").read_text(encoding="utf-8"))
    bindings = run_ledger.get("bindings", {}).get("bridge_windows", {})
    success_binding = bindings.get("bw_success", {})
    if not success_binding.get("packet_ref") or not success_binding.get("packet_hash"):
        raise AssertionError(json.dumps(success_binding, ensure_ascii=False, indent=2))
    return {"policy_team_projection": "passed"}


def run_outer_sdk_host_tests(control_root: Path, runtime_dir: Path) -> dict:
    repo_root = runtime_dir / "target_repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    config = OuterSdkHostConfig.from_values(
        control_root=control_root,
        repo_root=repo_root,
        default_main_session_id="outer_smoke_main",
    )
    host = OuterSdkHost(config, adapter=UnavailableOuterLeaderAdapter())
    response = host.handle_user_input(
        {
            "text": "read the current runtime status and report",
            "target_phase": "l3_bridge",
            "task_spec": {"task_subject": "outer host smoke", "task_kind": "preflight"},
        }
    )
    if response.get("accepted") is not True:
        raise AssertionError(json.dumps(response, ensure_ascii=False, indent=2))
    if response.get("leader_result", {}).get("error_or_null", {}).get("type") != "OuterLeaderSdkNotConfigured":
        raise AssertionError(json.dumps(response, ensure_ascii=False, indent=2))
    repo_key = response["host"]["repo_key"]
    run_id = response["host"]["run_id"]
    run_root = control_root.parent / "runtime_state" / "projects" / repo_key / "runs" / run_id
    event_log = _read_jsonl(run_root / "event_log.jsonl")
    if not any(item.get("event_kind") == "user_prompt_submitted" for item in event_log):
        raise AssertionError(json.dumps(event_log, ensure_ascii=False, indent=2))
    host_events = _read_jsonl(run_root / "outer_host_events.jsonl")
    if not any((item.get("runtime_event") or {}).get("source") == "outer_sdk" for item in host_events):
        raise AssertionError(json.dumps(host_events, ensure_ascii=False, indent=2))
    stream_events = _read_jsonl(run_root / "sdk_stream_events.jsonl")
    if not any(item.get("event_type") == "outer_user_input" for item in stream_events):
        raise AssertionError(json.dumps(stream_events, ensure_ascii=False, indent=2))
    sdk_adapter = ClaudeAgentSdkOuterLeaderAdapter(config)
    sdk_adapter._load_sdk = lambda: None
    sdk_missing = sdk_adapter.handle_user_input(
        {
            "repo_key": repo_key,
            "run_id": run_id,
            "main_session_id": "outer_smoke_main",
            "input_id": "in_sdk_missing",
            "text": "verify missing SDK dependency handling",
        }
    )
    if sdk_missing.get("error_or_null", {}).get("type") != "OuterLeaderSdkDependencyMissing":
        raise AssertionError(json.dumps(sdk_missing, ensure_ascii=False, indent=2))

    class FakeOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeTextBlock:
        def __init__(self, text: str):
            self.type = "text"
            self.text = text

    class FakeAssistantMessage:
        def __init__(self, text: str):
            self.content = [FakeTextBlock(text)]

    class FakeResultMessage:
        def __init__(self):
            self.subtype = "success"
            self.result = "fake sdk success"
            self.session_id = "outer_smoke_main"

    class FakeClaudeSDKClient:
        instances = []

        def __init__(self, *, options):
            self.options = options
            self.queries = []
            self.connected = False
            self._pending = []
            FakeClaudeSDKClient.instances.append(self)

        async def connect(self):
            self.connected = True

        async def query(self, prompt, session_id=None):
            self.queries.append({"prompt": prompt, "session_id": session_id})
            self._pending = [FakeAssistantMessage(f"fake response {len(self.queries)}"), FakeResultMessage()]

        async def receive_response(self):
            for message in self._pending:
                yield message
            self._pending = []

    class FakeSdkModule:
        ClaudeAgentOptions = FakeOptions
        ClaudeSDKClient = FakeClaudeSDKClient

    sdk_adapter = ClaudeAgentSdkOuterLeaderAdapter(config)
    sdk_adapter._load_sdk = lambda: FakeSdkModule
    sdk_events = []
    sdk_request = {
        "repo_key": repo_key,
        "run_id": run_id,
        "main_session_id": "outer_smoke_main",
        "input_id": "in_sdk_fake_1",
        "text": "first fake SDK input",
    }
    sdk_first = sdk_adapter.handle_user_input(sdk_request, event_sink=lambda event_type, payload, **kwargs: sdk_events.append({"event_type": event_type, "payload": payload, **kwargs}))
    sdk_request["input_id"] = "in_sdk_fake_2"
    sdk_request["text"] = "second fake SDK input"
    sdk_second = sdk_adapter.handle_user_input(sdk_request, event_sink=lambda event_type, payload, **kwargs: sdk_events.append({"event_type": event_type, "payload": payload, **kwargs}))
    if sdk_first.get("status") != "succeeded" or sdk_second.get("status") != "succeeded":
        raise AssertionError(json.dumps({"first": sdk_first, "second": sdk_second}, ensure_ascii=False, indent=2))
    if len(FakeClaudeSDKClient.instances) != 1:
        raise AssertionError(json.dumps({"client_instances": len(FakeClaudeSDKClient.instances)}, ensure_ascii=False, indent=2))
    fake_client = FakeClaudeSDKClient.instances[0]
    if [item.get("session_id") for item in fake_client.queries] != ["outer_smoke_main", "outer_smoke_main"]:
        raise AssertionError(json.dumps(fake_client.queries, ensure_ascii=False, indent=2))
    if not any(item.get("event_type") == "sdk_stream_assistant_text" for item in sdk_events):
        raise AssertionError(json.dumps(sdk_events, ensure_ascii=False, indent=2))
    if not fake_client.options.kwargs.get("mcp_servers", {}).get("bridge"):
        raise AssertionError(json.dumps(fake_client.options.kwargs, ensure_ascii=False, indent=2))
    if fake_client.options.kwargs.get("strict_mcp_config") is not True:
        raise AssertionError(json.dumps(fake_client.options.kwargs, ensure_ascii=False, indent=2))
    cli = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parent / "outer_sdk_host.py"),
            "--control-root",
            str(control_root),
            "--repo-root",
            str(repo_root),
            "--main-session-id",
            "outer_smoke_cli",
            "--adapter",
            "unavailable",
            "--input-json",
            json.dumps({"text": "record a CLI host input", "input_kind": "user_prompt"}, ensure_ascii=False),
        ],
        cwd=str(runtime_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if cli.returncode != 0:
        raise AssertionError(json.dumps({"stdout": cli.stdout, "stderr": cli.stderr}, ensure_ascii=False, indent=2))
    cli_response = json.loads(cli.stdout)
    if cli_response.get("accepted") is not True or cli_response.get("host", {}).get("adapter") != "unavailable":
        raise AssertionError(json.dumps(cli_response, ensure_ascii=False, indent=2))
    return {"outer_sdk_host": "passed"}


def main() -> None:
    workspace_tmp = Path.cwd() / ".runtime_smoke_tmp"
    workspace_tmp.mkdir(parents=True, exist_ok=True)
    runtime_dir = workspace_tmp / f"claude_bridge_workflow_smoke_{uuid.uuid4().hex[:8]}"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    try:
        control_root, runs_root = build_fixture(runtime_dir)
        user_clarification = run_user_clarification_resume(control_root, runs_root)
        success = run_success(control_root, runs_root)
        if success.get("current_phase") != "l4_execute":
            raise AssertionError(json.dumps({"expected_phase": "l4_execute", "snapshot": success}, ensure_ascii=False, indent=2))
        bridge_boundary = run_bridge_boundary_tests(control_root, runs_root)
        event_artifact_completion = run_event_artifact_completion_tests(control_root, runs_root)
        outer_sdk_host = run_outer_sdk_host_tests(control_root, runtime_dir)
        state_graph = run_state_graph_tests(control_root, runs_root)
        retry_control_root, retry_runs_root = build_fixture(runtime_dir / "retry")
        retry_policy = run_retry_policy_tests(retry_control_root, retry_runs_root)
        guardrail_control_root, guardrail_runs_root = build_fixture(runtime_dir / "guardrail")
        guardrails = run_guardrail_tests(guardrail_control_root, guardrail_runs_root)
        failure = run_failure(control_root, runs_root)
        orphan = run_orphan(control_root, runs_root)
        mcp_helper = run_mcp_lifecycle_helper(control_root, runs_root)
        sdk = run_sdk_roundtrip(control_root, runs_root)
        policy_team_projection = run_policy_team_projection_tests(control_root, runs_root)
        anomaly_control_root, anomaly_runs_root = build_fixture(runtime_dir / "anomaly")
        stuck_dispatch = run_stuck_dispatch_anomaly(anomaly_control_root, anomaly_runs_root)
        watchdog_control_root, watchdog_runs_root = build_fixture(runtime_dir / "watchdog")
        execute_watchdog = run_execute_watchdog_alert(watchdog_control_root, watchdog_runs_root)
        interrupt_control_root, interrupt_runs_root = build_fixture(runtime_dir / "interrupt")
        manual_interrupt = run_manual_interrupt_recovery(interrupt_control_root, interrupt_runs_root)
        negative_control_root, negative_runs_root = build_fixture(runtime_dir / "negative")
        negative = run_negative_tests(negative_control_root, negative_runs_root)
        cli_executor = run_cli_executor_policy_tests(runtime_dir)
        hook_observer = run_hook_observer_rebind_tests(runtime_dir, runs_root)
        checkpoint_trajectory = run_checkpoint_trajectory_tests(runs_root)
        checkpoint_order_control_root, checkpoint_order_runs_root = build_fixture(runtime_dir / "checkpoint_order")
        checkpoint_write_order = run_checkpoint_write_order_test(checkpoint_order_control_root, checkpoint_order_runs_root)
        multi_repo = run_multi_repo_isolation_tests(runtime_dir, control_root)
        summary = {
            "success_status": success["lifecycle"]["status_index"]["bw_success"],
            "success_phase": success["current_phase"],
            "failure_status": failure["lifecycle"]["status_index"]["bw_failed"],
            "orphan_status": orphan["lifecycle"]["status_index"]["bw_orphan"],
            "user_clarification_status": user_clarification["lifecycle"]["status_index"]["bw_user_clarification"],
            "user_clarification_allowed_actions": user_clarification["allowed_actions"],
            "l3_allowed_routes": user_clarification["allowed_routes"],
            "mcp_helper_status": mcp_helper["lifecycle"]["status_index"]["bw_mcp_helper"],
            "stuck_dispatch_anomaly": stuck_dispatch["runtime_diagnostics"]["orchestration_anomalies"][0]["classification"],
            "execute_watchdog_alert": execute_watchdog["runtime_diagnostics"]["execute_watchdog_alerts"][0]["classification"],
            "manual_interrupt_status": manual_interrupt["lifecycle"]["status_index"]["bw_manual_interrupt"],
            "sdk_status": sdk["bridge_result_status"],
            "sdk_replay_status": sdk["replay_status"],
            "sdk_failed_status": sdk["failed_status"],
            "sdk_exception_status": sdk["exception_status"],
            "sdk_partial_status": sdk["partial_status"],
            "sdk_running_partial_status": sdk["running_partial_status"],
            "sdk_reject_status": sdk["reject_status"],
            "sdk_manifest_status": sdk["manifest_status"],
            "negative_tests": negative["negative_tests"],
            "cli_executor_policy": cli_executor["cli_executor_policy"],
            "hook_observer_rebind": hook_observer["hook_observer_rebind"],
            "state_graph": state_graph["state_graph"],
            "retry_policy": retry_policy["retry_policy"],
            "guardrails": guardrails["guardrails"],
            "bridge_boundary": bridge_boundary["bridge_boundary"],
            "event_artifact_completion": event_artifact_completion["event_artifact_completion"],
            "outer_sdk_host": outer_sdk_host["outer_sdk_host"],
            "policy_team_projection": policy_team_projection["policy_team_projection"],
            "checkpoint_trajectory": checkpoint_trajectory["checkpoint_trajectory"],
            "checkpoint_write_order": checkpoint_write_order["checkpoint_write_order"],
            "multi_repo_isolation": multi_repo["multi_repo_isolation"],
            "open_bridge_window_ids": orphan["lifecycle"]["open_bridge_window_ids"],
            "inbox_exists": (runs_root / "run_demo" / "main_leader_inbox.jsonl").exists(),
            "runtime_dir": str(runtime_dir),
        }
        assert summary["success_status"] == "bridge_window_returned"
        assert summary["failure_status"] == "bridge_call_failed"
        assert summary["orphan_status"] == "bridge_window_orphaned"
        assert summary["user_clarification_status"] == "continuation_of_previous_l3"
        assert "call_bridge_sdk" in summary["user_clarification_allowed_actions"]
        assert {"l3_bridge", "leader_freeze", "l2_advisory", "l4_implement", "l4_execute", "l4_anomaly"}.issubset(set(summary["l3_allowed_routes"]))
        assert summary["mcp_helper_status"] == "bridge_window_orphaned"
        assert summary["stuck_dispatch_anomaly"] == "bridge_orchestration_hang"
        assert summary["execute_watchdog_alert"] == "execute_stale_heartbeat_with_owned_process_refs"
        assert summary["manual_interrupt_status"] == "bridge_window_interrupted"
        assert summary["sdk_status"] == "succeeded"
        assert summary["sdk_replay_status"] == "bridge_window_returned"
        assert summary["sdk_failed_status"] == "bridge_window_failed"
        assert summary["sdk_exception_status"] == "bridge_window_failed"
        assert summary["sdk_partial_status"] == "bridge_window_partial_returned"
        assert summary["sdk_running_partial_status"] == "bridge_window_failed"
        assert summary["sdk_reject_status"] == "bridge_window_failed"
        assert summary["sdk_manifest_status"] == "bridge_window_returned"
        assert summary["state_graph"] == "passed"
        assert summary["retry_policy"] == "passed"
        assert summary["guardrails"] == "passed"
        assert summary["bridge_boundary"] == "passed"
        assert summary["event_artifact_completion"] == "passed"
        assert summary["outer_sdk_host"] == "passed"
        assert summary["policy_team_projection"] == "passed"
        assert summary["checkpoint_trajectory"] == "passed"
        assert summary["checkpoint_write_order"] == "passed"
        assert summary["multi_repo_isolation"] == "passed"
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)
        try:
            workspace_tmp.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
