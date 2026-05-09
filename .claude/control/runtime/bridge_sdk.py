from __future__ import annotations

from pathlib import Path
from typing import Any

from bridge_leader import TeamExecutor, execute_bridge_window
from workflow_runtime import dispatch_workflow_event


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
    binding = packet.get("binding", {})
    if record_main_lifecycle:
        _record_main_bridge_start(control_root, packet, runtime_runs_root=runtime_runs_root, persist=persist)
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
