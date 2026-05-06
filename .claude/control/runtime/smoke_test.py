from __future__ import annotations

import json
import shutil
import sys
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import uuid

from bridge_sdk import call_bridge_sdk
from claude_cli_executor import _allowed_tools
from claude_cli_executor import _bridge_leader_prompt
from claude_cli_executor import _ensure_project_agent_files
from claude_cli_executor import _parse_claude_payload
from claude_cli_executor import _redact_cmd
from claude_cli_executor import simulated_team_executor
from main_leader import decide_next_bridge_packet
from workflow_runtime import dispatch_workflow_event
from workflow_runtime import reconcile_workflow_from_ledger


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
            {"name": "l3_bridge", "allowed_next_phases": ["l3_bridge", "leader_freeze", "l4_implement", "l4_execute", "l4_anomaly"]},
            {"name": "l4_implement", "allowed_next_phases": ["l4_execute", "l4_anomaly"]},
            {"name": "l4_execute", "allowed_next_phases": ["l4_anomaly"]},
            {"name": "l4_anomaly", "allowed_next_phases": ["l4_implement", "l4_execute"]},
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
    _write_json(control_root / "policy" / "approval_matrix.json", {"categories": {}})
    _write_json(control_root / "policy" / "reconcile_rules.json", {"schema_version": "0.4.0"})
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
            "teammate_specs": [{"teammate_name": "worker", "role": "execute", "allowed_tools": [], "responsibilities": []}],
            "ownership_boundary": {"readable_scopes": [], "writable_scopes": ["tmp"], "process_ownership_rules": [], "forbidden_actions": []},
        },
        "task_spec": {
            "task_id_or_null": None,
            "task_subject": f"task_{bridge_window_id}",
            "task_description": "smoke task",
            "task_kind": "bridge_window_smoke",
            "target_phase": "l4_execute",
            "completion_contract": {
                "required_outputs": ["report"],
                "required_artifacts": ["artifact"],
                "validation_requirements": ["validated"],
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
                "required_sections": ["summary", "evidence"],
                "required_evidence": ["runtime event ids", "artifact"],
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
            "required_artifacts": ["artifact"],
            "validation_requirements": ["validated"],
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
            "required_sections": ["summary", "evidence"],
            "required_evidence": ["runtime event ids", "artifact"],
            "artifact_reporting_format": "list",
            "include_failure_reason": True,
            "include_next_action_recommendation": True,
        },
        "allowed_actions": ["team_create", "task_create", "send_messages", "task_complete", "team_delete"],
        "allowed_tools": [],
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
        "timestamp": _now(),
        "payload": payload,
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
    dispatch(control_root, runs_root, event("completion_contract_satisfied", bw, ss, team_id="team_success", task_id="task_success", agent_type="hook", agent_id="hook.task_completed", payload={"completion_contract": p["completion_contract"], "completion_checks": {"required_outputs_present": True, "required_artifacts_present": True, "validation_passed": True, "missing_outputs": [], "missing_artifacts": [], "failed_validations": [], "notes": []}, "reports": [{"summary": "ok"}], "artifact_refs": ["artifact"]}))
    dispatch(control_root, runs_root, event("team_delete_started", bw, ss, team_id="team_success", task_id="task_success", tool_name="team_delete"))
    dispatch(control_root, runs_root, event("team_delete_succeeded", bw, ss, team_id="team_success", task_id="task_success", tool_name="team_delete"))
    return dispatch(control_root, runs_root, event("bridge_result_returned", bw, ss, team_id="team_success", task_id="task_success", agent_type="main-leader", agent_id="main", tool_name="call_bridge_sdk", payload={"bridge_result": {"status": "succeeded", "reports": [{"summary": "ok"}], "artifact_refs": ["artifact"]}}))


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


def run_user_clarification_resume(control_root: Path, runs_root: Path) -> dict:
    bw = "bw_user_clarification"
    ss = "sub_user_clarification"
    p = packet(bw, ss)
    p["target_phase"] = "l3_bridge"
    p["phase_route"] = ["l3_bridge"]
    p["task_spec"]["target_phase"] = "l3_bridge"
    p["team_spec"]["ownership_boundary"]["writable_scopes"] = ["CLAUDE.md", "README.md", "docs/", "*.md"]
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
        team_executor=lambda _: {"status": "partial", "reports": [{"summary": "partial"}], "evidence": {"reason": "intentional partial"}},
    )
    if partial_result.get("status") != "partial_or_failed":
        raise AssertionError(json.dumps(partial_result, ensure_ascii=False, indent=2))

    reject_packet = decide_next_bridge_packet(
        str(control_root),
        "run_demo",
        runtime_runs_root=str(runs_root),
        main_session_id="main_demo",
        user_instruction="run rejected completion sdk bridge smoke task",
        task_spec={"task_subject": "sdk bridge reject smoke", "task_kind": "bridge_window_smoke"},
    )
    reject_packet["completion_contract"]["required_artifacts"] = ["artifact"]
    reject_packet["completion_contract"]["success_criteria"] = ["artifact required"]
    reject_result = call_bridge_sdk(str(control_root), reject_packet, runtime_runs_root=str(runs_root), persist=True, team_executor=simulated_team_executor)
    if reject_result.get("status") != "failed":
        raise AssertionError(json.dumps(reject_result, ensure_ascii=False, indent=2))

    replay = reconcile_workflow_from_ledger(str(control_root), "run_demo", runtime_runs_root=str(runs_root), persist=True)
    failed_status = replay["runtime_snapshot"]["lifecycle"]["status_index"][failed_packet["binding"]["bridge_window_id"]]
    exception_status = replay["runtime_snapshot"]["lifecycle"]["status_index"][exception_packet["binding"]["bridge_window_id"]]
    partial_status = replay["runtime_snapshot"]["lifecycle"]["status_index"][partial_packet["binding"]["bridge_window_id"]]
    reject_status = replay["runtime_snapshot"]["lifecycle"]["status_index"][reject_packet["binding"]["bridge_window_id"]]
    return {
        "bridge_window_id": packet["binding"]["bridge_window_id"],
        "bridge_result_status": bridge_result["status"],
        "replay_status": status,
        "failed_status": failed_status,
        "exception_status": exception_status,
        "partial_status": partial_status,
        "reject_status": reject_status,
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
    bad_l3_scope["team_spec"]["ownership_boundary"]["writable_scopes"] = ["."]
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
    if "instruction_coverage" not in set(hardened_l3["report_contract"].get("required_sections", [])):
        raise AssertionError(json.dumps(hardened_l3["report_contract"], ensure_ascii=False, indent=2))
    if "instruction coverage disposition" not in set(hardened_l3["report_contract"].get("required_evidence", [])):
        raise AssertionError(json.dumps(hardened_l3["report_contract"], ensure_ascii=False, indent=2))
    l3_boundary = hardened_l3["team_spec"]["ownership_boundary"]
    if set(l3_boundary.get("writable_scopes", [])) != {"CLAUDE.md", "README.md", "docs/", "*.md"}:
        raise AssertionError(json.dumps(l3_boundary, ensure_ascii=False, indent=2))
    l3_assignments = "\n".join(
        str(item.get("assignment") or "")
        for item in hardened_l3.get("task_team_mapping", {}).get("teammate_assignments", [])
        if isinstance(item, dict)
    )
    if (
        "CLAUDE.md" not in l3_assignments
        or "smallest correct documentation update" not in l3_assignments
        or "Instruction coverage checklist" not in l3_assignments
        or "do not mark the task complete until every checklist item is completed" not in l3_assignments
        or "extra context must survive normalization" not in l3_assignments
    ):
        raise AssertionError(json.dumps(hardened_l3.get("task_team_mapping"), ensure_ascii=False, indent=2))
    l3_team_tools = {
        tool
        for teammate in hardened_l3["team_spec"]["teammate_specs"]
        for tool in teammate.get("allowed_tools", [])
    }
    if "Write" not in set(hardened_l3.get("allowed_tools", [])) or "Write" not in l3_team_tools:
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

    execute_packet = decide_next_bridge_packet(
        str(control_root),
        "run_demo",
        runtime_runs_root=str(runs_root),
        main_session_id="main_demo",
        user_instruction="long formal execution timeout smoke",
        target_phase="l4_execute",
    )
    execute_timeout = execute_packet["completion_contract"]["timeout_policy"]
    if execute_timeout.get("soft_timeout_seconds", 0) < 3600 or execute_timeout.get("hard_timeout_seconds", 0) < 14400:
        raise AssertionError(json.dumps(execute_timeout, ensure_ascii=False, indent=2))
    if execute_packet["task_spec"]["completion_contract"]["timeout_policy"] != execute_timeout:
        raise AssertionError(json.dumps(execute_packet["task_spec"]["completion_contract"], ensure_ascii=False, indent=2))
    execute_assignments = "\n".join(
        str(item.get("assignment") or "")
        for item in execute_packet.get("task_team_mapping", {}).get("teammate_assignments", [])
        if isinstance(item, dict)
    )
    if "estimate expected wall-clock runtime" not in execute_assignments:
        raise AssertionError(json.dumps(execute_packet.get("task_team_mapping"), ensure_ascii=False, indent=2))
    execute_assignments = json.dumps(execute_packet["task_team_mapping"]["teammate_assignments"], ensure_ascii=False)
    if "near-ceiling accelerator memory utilization" not in execute_assignments or "resource utilization" not in execute_assignments:
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
                    "reports": [{"summary": "ok"}],
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
    return {"cli_executor_policy": "passed"}


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
        failure = run_failure(control_root, runs_root)
        orphan = run_orphan(control_root, runs_root)
        mcp_helper = run_mcp_lifecycle_helper(control_root, runs_root)
        sdk = run_sdk_roundtrip(control_root, runs_root)
        negative_control_root, negative_runs_root = build_fixture(runtime_dir / "negative")
        negative = run_negative_tests(negative_control_root, negative_runs_root)
        cli_executor = run_cli_executor_policy_tests(runtime_dir)
        summary = {
            "success_status": success["lifecycle"]["status_index"]["bw_success"],
            "success_phase": success["current_phase"],
            "failure_status": failure["lifecycle"]["status_index"]["bw_failed"],
            "orphan_status": orphan["lifecycle"]["status_index"]["bw_orphan"],
            "user_clarification_status": user_clarification["lifecycle"]["status_index"]["bw_user_clarification"],
            "user_clarification_allowed_actions": user_clarification["allowed_actions"],
            "l3_allowed_routes": user_clarification["allowed_routes"],
            "mcp_helper_status": mcp_helper["lifecycle"]["status_index"]["bw_mcp_helper"],
            "sdk_status": sdk["bridge_result_status"],
            "sdk_replay_status": sdk["replay_status"],
            "sdk_failed_status": sdk["failed_status"],
            "sdk_exception_status": sdk["exception_status"],
            "sdk_partial_status": sdk["partial_status"],
            "sdk_reject_status": sdk["reject_status"],
            "negative_tests": negative["negative_tests"],
            "cli_executor_policy": cli_executor["cli_executor_policy"],
            "open_bridge_window_ids": orphan["lifecycle"]["open_bridge_window_ids"],
            "inbox_exists": (runs_root / "run_demo" / "main_leader_inbox.jsonl").exists(),
            "runtime_dir": str(runtime_dir),
        }
        assert summary["success_status"] == "bridge_window_returned"
        assert summary["failure_status"] == "bridge_call_failed"
        assert summary["orphan_status"] == "bridge_window_orphaned"
        assert summary["user_clarification_status"] == "continuation_of_previous_l3"
        assert "call_bridge_sdk" in summary["user_clarification_allowed_actions"]
        assert "l3_bridge" in summary["l3_allowed_routes"] and "leader_freeze" in summary["l3_allowed_routes"]
        assert summary["mcp_helper_status"] == "bridge_window_orphaned"
        assert summary["sdk_status"] == "succeeded"
        assert summary["sdk_replay_status"] == "bridge_window_returned"
        assert summary["sdk_failed_status"] == "bridge_window_failed"
        assert summary["sdk_exception_status"] == "bridge_window_failed"
        assert summary["sdk_partial_status"] == "bridge_window_partial_returned"
        assert summary["sdk_reject_status"] == "bridge_window_failed"
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
