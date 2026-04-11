from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from dispatch import dispatch_action
from models import ActionRequest
from persist import persist_dispatch_result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_fixture(root: Path) -> tuple[Path, Path]:
    control_root = root / "control"
    runs_root = control_root / "runtime_state" / "runs"
    run_root = runs_root / "run_demo"
    (control_root / "policy").mkdir(parents=True, exist_ok=True)
    (run_root / "tasks").mkdir(parents=True, exist_ok=True)

    phase_graph = {
        "phases": [
            {"name": "leader_freeze", "allowed_next_phases": ["l3_bridge"]},
            {"name": "l2_advisory", "allowed_next_phases": ["l3_bridge"]},
            {"name": "l3_bridge", "allowed_next_phases": ["l4_implement", "l4_execute"]},
            {"name": "l4_implement", "allowed_next_phases": ["l4_execute", "l4_anomaly"]},
            {"name": "l4_execute", "allowed_next_phases": ["l4_anomaly"]},
            {"name": "l4_anomaly", "allowed_next_phases": ["l4_implement", "l4_execute"]},
        ],
        "completion_policy": {
            "run_may_complete_from_phases": ["l3_bridge", "l4_execute", "l4_anomaly"]
        },
    }
    approval_matrix = {"categories": {}}
    reconcile_rules = {"name": "smoke"}

    now = _now()
    run_ledger = {
        "schema_version": "0.3.0",
        "run_id": "run_demo",
        "workflow_name": "demo",
        "workflow_version": "0.3.0",
        "run_status": "in_progress",
        "current_phase": "l3_bridge",
        "phase_history": [
            {
                "phase": "leader_freeze",
                "entered_at": now,
                "exited_at": now,
                "status": "completed",
                "entry_reason": "",
                "exit_reason": "",
                "trigger_task_id": None,
                "transition_in_id": None,
                "transition_out_id": None,
            },
            {
                "phase": "l3_bridge",
                "entered_at": now,
                "exited_at": None,
                "status": "entered",
                "entry_reason": "",
                "exit_reason": "",
                "trigger_task_id": None,
                "transition_in_id": None,
                "transition_out_id": None,
            },
        ],
        "task_index": {
            "all_task_ids": [],
            "open_task_ids": [],
            "active_task_ids": [],
            "waiting_task_ids": [],
            "blocked_task_ids": [],
            "retryable_task_ids": [],
            "completed_task_ids": [],
            "failed_task_ids": [],
            "aborted_task_ids": [],
            "noop_task_ids": [],
            "phase_gate_task_ids": [],
        },
        "phase_task_index": {
            phase: {
                "all_task_ids": [],
                "open_task_ids": [],
                "active_task_ids": [],
                "blocked_task_ids": [],
                "completed_task_ids": [],
            }
            for phase in ["leader_freeze", "l2_advisory", "l3_bridge", "l4_implement", "l4_execute", "l4_anomaly"]
        },
        "allowed_next_actions": ["create_task", "complete_task", "advance_phase", "complete_run", "abort_run"],
        "allowed_next_phases": ["l4_implement", "l4_execute"],
        "approval_state": {"pending": False, "active_approval_ids": [], "records": []},
        "hard_stop": {"active": False, "reason_code": None, "details": None, "task_id": None, "raised_at": None},
        "authoritative_artifacts": [],
        "checkpoint_ref": None,
        "last_transition_id": None,
        "completion_summary": {
            "completion_eligible": False,
            "blocking_open_required_phase_gate_task_ids": [],
            "pending_approval_blocks_completion": False,
            "hard_stop_blocks_completion": False,
            "finalized_at": None,
        },
        "phase_exit_readiness": {"current_phase": "l3_bridge", "exit_ready": False, "blocking_phase_gate_task_ids": []},
        "created_at": now,
        "updated_at": now,
        "closed_at": None,
    }

    _write_json(control_root / "policy" / "phase_graph.json", phase_graph)
    _write_json(control_root / "policy" / "approval_matrix.json", approval_matrix)
    _write_json(control_root / "policy" / "reconcile_rules.json", reconcile_rules)
    _write_json(run_root / "run_ledger.json", run_ledger)
    (run_root / "transitions.jsonl").write_text("", encoding="utf-8")
    return control_root, runs_root


def main() -> None:
    runtime_dir = Path(tempfile.mkdtemp(prefix="claude_control_smoke_"))
    control_root, runs_root = build_fixture(runtime_dir)

    create_req = ActionRequest(
        run_id="run_demo",
        action="create_task",
        payload={
            "task_id": "task_bridge_gate",
            "task_group": "l3_bridge",
            "task_kind": "bridge_preflight_gate",
            "objective": "Bridge gate task",
            "phase_gate": True,
            "acceptance_contract": {
                "required_outputs": [],
                "validation_requirements": [],
                "phase_exit_relevant": True,
            },
            "required_artifacts": [],
            "completion_checks": {
                "required_outputs_present": False,
                "required_artifacts_present": False,
                "validation_passed": False,
                "missing_outputs": [],
                "missing_artifacts": [],
                "failed_validations": [],
                "notes": [],
            },
        },
        reason="smoke create",
        timestamp=_now(),
    )
    create_result = dispatch_action(str(control_root), create_req)
    persist_dispatch_result(str(control_root), create_result)

    complete_req = ActionRequest(
        run_id="run_demo",
        action="complete_task",
        task_id="task_bridge_gate",
        payload={
            "completion_checks": {
                "required_outputs_present": True,
                "required_artifacts_present": True,
                "validation_passed": True,
                "missing_outputs": [],
                "missing_artifacts": [],
                "failed_validations": [],
                "notes": [],
            }
        },
        reason="smoke complete",
        timestamp=_now(),
    )
    complete_result = dispatch_action(str(control_root), complete_req)
    persist_dispatch_result(str(control_root), complete_result)

    advance_req = ActionRequest(
        run_id="run_demo",
        action="advance_phase",
        payload={"target_phase": "l4_implement"},
        reason="smoke advance",
        timestamp=_now(),
    )
    advance_result = dispatch_action(str(control_root), advance_req)
    persist_dispatch_result(str(control_root), advance_result)

    summary = {
        "create_decision": create_result.decision,
        "complete_decision": complete_result.decision,
        "advance_decision": advance_result.decision,
        "final_phase": advance_result.run_ledger["current_phase"],
        "final_run_status": advance_result.run_ledger["run_status"],
        "allowed_next_actions": advance_result.run_ledger["allowed_next_actions"],
        "runtime_dir": str(runtime_dir),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
