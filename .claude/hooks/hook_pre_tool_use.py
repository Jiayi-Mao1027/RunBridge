from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_RUNTIME_DIR = Path(__file__).resolve().parents[1] / "control" / "runtime"
if str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))

from common import (
    compact_tool_summary,
    compact_tool_target,
    control_binding_value,
    control_main_session_id,
    bash_execution_soft_reminders,
    detect_run_id,
    emit_observer_record,
    invoke_runtime_event,
    load_last_bridge_packet,
    now_iso,
    normalized_tool_input,
    observer_binding,
    pretool_deny,
    read_hook_input,
    safe_input_preview,
    tool_detail_fields,
    tool_file_refs,
)
from dispatch_contract import validate_agent_call_against_dispatch_contract


BRIDGE_TOOL_NAMES = {"call_bridge_sdk", "mcp__bridge__call_bridge_sdk"}
START_BY_TOOL = {
    "team_create": "team_create_started",
    "task_create": "task_create_started",
    "send_messages": "message_dispatch_started",
    "send_message": "message_dispatch_started",
    "task_complete": "artifacts_ready",
    "team_delete": "team_delete_started",
}
GENERIC_AGENT_MODEL_ALIASES = {"haiku", "sonnet", "opus"}


def main() -> int:
    payload = read_hook_input()
    tool_name = str(payload.get("tool_name") or "").strip()
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if tool_name:
        _emit_tool_event(payload, tool_input, tool_name, status="started")
    if tool_name == "Agent":
        contract_denial = _bridge_agent_contract_denial(payload, tool_input)
        if contract_denial:
            reason, model_value, guard = contract_denial
            _emit_agent_model_guard_denial(payload, tool_input, model_value, reason, guard)
            return pretool_deny(reason)
    if tool_name not in BRIDGE_TOOL_NAMES and tool_name not in START_BY_TOOL:
        return 0

    packet = tool_input.get("packet") if isinstance(tool_input, dict) else None
    tool_arguments = tool_input.get("arguments") if isinstance(tool_input.get("arguments"), dict) else {}
    if packet is None and isinstance(tool_arguments, dict):
        packet = tool_arguments.get("packet")
    if packet is None and tool_name in BRIDGE_TOOL_NAMES:
        # No explicit packet means the MCP server must derive the current
        # run's packet from runtime-owned context. Do not preflight a stale
        # saved packet from an earlier user input; that can block semantic
        # refresh before the server has a chance to rebuild the packet.
        return 0

    run_payload = dict(payload)
    if packet is not None:
        run_payload["packet"] = packet
    run_id = detect_run_id(run_payload)
    if not run_id:
        return pretool_deny("runtime run binding missing; SessionStart active-run is required before bridge lifecycle tools")

    bridge_window_id = (
        tool_input.get("bridge_window_id")
        or payload.get("bridge_window_id")
        or (packet or {}).get("binding", {}).get("bridge_window_id")
        or control_binding_value("bridge_window_id", payload, tool_input, packet)
    )
    sub_session_id = (
        tool_input.get("sub_session_id")
        or payload.get("sub_session_id")
        or (packet or {}).get("binding", {}).get("sub_session_id")
        or control_binding_value("sub_session_id", payload, tool_input, packet)
    )
    tool_use_id = str(payload.get("tool_use_id") or tool_input.get("tool_use_id") or "").strip() or None

    if tool_name in START_BY_TOOL:
        event = {
            "run_id": run_id,
            "main_session_id": control_main_session_id(payload, tool_input, packet),
            "sub_session_id": sub_session_id,
            "bridge_window_id": bridge_window_id,
            "team_id": payload.get("team_id") or tool_input.get("team_id") or control_binding_value("team_id", payload, tool_input, packet),
            "task_id": payload.get("task_id") or tool_input.get("task_id") or control_binding_value("task_id", payload, tool_input, packet),
            "agent_id": payload.get("agent_id") or tool_input.get("agent_id") or "bridge-leader",
            "agent_type": payload.get("agent_type") or tool_input.get("agent_type") or "bridge-leader",
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "event_kind": START_BY_TOOL[tool_name],
            "timestamp": now_iso(),
            "payload": {"tool_input": tool_input, "packet": packet},
        }
        code, result, stderr = invoke_runtime_event(event, persist=True)
        if code != 0:
            return pretool_deny(f"{START_BY_TOOL[tool_name]} runtime failure: {stderr or result!r}")
        workflow = result.get("workflow_result", {})
        if not workflow.get("ok", False):
            check = workflow.get("check_result", {})
            return pretool_deny(f"{START_BY_TOOL[tool_name]} denied: {check.get('reasons')}")
        return 0

    intent_event = {
        "run_id": run_id,
        "main_session_id": control_main_session_id(payload, tool_input, packet),
        "sub_session_id": sub_session_id,
        "bridge_window_id": bridge_window_id,
        "agent_id": payload.get("agent_id") or "main-leader",
        "agent_type": payload.get("agent_type") or "main-leader",
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "event_kind": "bridge_call_intended",
        "timestamp": now_iso(),
        "payload": {"packet": packet, "tool_input": tool_input},
    }
    code, result, stderr = invoke_runtime_event(intent_event, persist=True)
    if code != 0:
        return pretool_deny(f"bridge_call_intended runtime failure: {stderr or result!r}")
    workflow = result.get("workflow_result", {})
    if not workflow.get("ok", False):
        check = workflow.get("check_result", {})
        denied_event = dict(intent_event)
        denied_event["event_kind"] = "pretooluse_denied_by_main_leader"
        denied_event.pop("event_id", None)
        denied_event["timestamp"] = now_iso()
        denied_event["payload"] = {"packet": packet, "tool_input": tool_input, "reasons": check.get("reasons", [])}
        invoke_runtime_event(denied_event, persist=True)
        return pretool_deny(f"bridge call denied at intent: {check.get('reasons')}")

    prechecked_event = dict(intent_event)
    prechecked_event["event_kind"] = "pretooluse_allowed_by_main_leader"
    prechecked_event.pop("event_id", None)
    prechecked_event["timestamp"] = now_iso()
    code, result, stderr = invoke_runtime_event(prechecked_event, persist=True)
    if code != 0:
        return pretool_deny(f"bridge precheck runtime failure: {stderr or result!r}")
    workflow = result.get("workflow_result", {})
    if not workflow.get("ok", False):
        check = workflow.get("check_result", {})
        denied_event = dict(intent_event)
        denied_event["event_kind"] = "pretooluse_denied_by_main_leader"
        denied_event.pop("event_id", None)
        denied_event["timestamp"] = now_iso()
        denied_event["payload"] = {"packet": packet, "tool_input": tool_input, "reasons": check.get("reasons", [])}
        invoke_runtime_event(denied_event, persist=True)
        return pretool_deny(f"bridge call denied: {check.get('reasons')}")

    started_event = dict(intent_event)
    started_event["event_kind"] = "call_bridge_sdk_started"
    started_event.pop("event_id", None)
    started_event["timestamp"] = now_iso()
    code, result, stderr = invoke_runtime_event(started_event, persist=True)
    if code != 0:
        return pretool_deny(f"bridge start runtime failure: {stderr or result!r}")
    workflow = result.get("workflow_result", {})
    if not workflow.get("ok", False):
        check = workflow.get("check_result", {})
        return pretool_deny(f"bridge start denied: {check.get('reasons')}")

    return 0


def _bridge_agent_contract_denial(payload: dict, tool_input: dict) -> tuple[str, str, dict | None] | None:
    if not _bridge_agent_context(payload, tool_input):
        denied_alias = _generic_agent_model_alias(tool_input)
        reason = (
            "Direct Agent dispatch is not allowed outside a bridge window. "
            "Main leader must use mcp__bridge__build_bridge_packet and "
            "mcp__bridge__call_bridge_sdk; teammate Agent calls are only legal "
            "inside bridge windows through system-owned dispatch_contract."
        )
        if denied_alias:
            reason = (
                f"Agent model guard denied generic model alias {denied_alias!r}; "
                + reason
            )
        return (reason, denied_alias or str(tool_input.get("model") or ""), None)

    run_id = detect_run_id(payload)
    if not run_id:
        guard = {
            "reason_codes": ["agent_dispatch_run_id_missing"],
            "actual_input_keys": _safe_sorted_keys(tool_input),
            "actual_subagent_type": _safe_scalar(tool_input.get("subagent_type")),
            "actual_description": _safe_scalar(tool_input.get("description")),
            "actual_model": _safe_scalar(tool_input.get("model")),
        }
        return (
            "Bridge teammate Agent call is missing a run_id; refusing to fall back to a project-global packet. "
            "Bridge Agent dispatch must be bound to the run-scoped dispatch_contract.",
            str(tool_input.get("model") or ""),
            guard,
        )
    packet = load_last_bridge_packet(run_id)
    binding = _agent_contract_binding(payload, tool_input)
    reasons = validate_agent_call_against_dispatch_contract(packet, tool_input, binding)
    if reasons:
        guard = _agent_contract_guard(packet, tool_input, binding, reasons)
        canonical_hint = ""
        if guard.get("canonical_subagent_type"):
            canonical_hint = (
                f" Canonical subagent_type: {guard['canonical_subagent_type']!r}."
            )
        return (
            "Bridge teammate Agent call violates system-owned dispatch_contract: "
            + ", ".join(reasons)
            + ". "
            + canonical_hint
            + " Use only the exact agent_dispatch object already present in the packet; do not reconstruct "
            "subagent_type, description, prompt, model, or wrapper fields. If all required teammate reports "
            "are already available, stop dispatching Agents and return the BridgeResult from existing reports. "
            "Full guard details were recorded in observer streams.",
            str(tool_input.get("model") or ""),
            guard,
        )
    return None


def _bridge_agent_context(payload: dict, tool_input: dict) -> bool:
    if str(payload.get("bridge_window_id") or tool_input.get("bridge_window_id") or "").strip():
        return True
    for key in ("BRIDGE_WINDOW_ID", "BRIDGE_TEAM_ID", "BRIDGE_TASK_ID", "BRIDGE_CHILD_CLAUDE_SESSION"):
        if str(os.environ.get(key) or "").strip():
            return True
    return False


def _agent_contract_binding(payload: dict, tool_input: dict) -> dict[str, str | None]:
    return {
        "run_id": payload.get("run_id") or tool_input.get("run_id") or os.environ.get("BRIDGE_RUN_ID"),
        "main_session_id": payload.get("main_session_id") or tool_input.get("main_session_id") or os.environ.get("BRIDGE_MAIN_SESSION_ID"),
        "sub_session_id": payload.get("sub_session_id") or tool_input.get("sub_session_id") or os.environ.get("BRIDGE_SUB_SESSION_ID"),
        "bridge_window_id": payload.get("bridge_window_id") or tool_input.get("bridge_window_id") or os.environ.get("BRIDGE_WINDOW_ID"),
        "team_id": payload.get("team_id") or tool_input.get("team_id") or os.environ.get("BRIDGE_TEAM_ID"),
        "task_id": payload.get("task_id") or tool_input.get("task_id") or os.environ.get("BRIDGE_TASK_ID"),
    }


def _agent_contract_guard(packet: dict, tool_input: dict, binding: dict[str, str | None], reasons: list[str]) -> dict:
    contract = packet.get("dispatch_contract") if isinstance(packet, dict) else None
    contract = contract if isinstance(contract, dict) else {}
    teammates = contract.get("teammates") if isinstance(contract.get("teammates"), dict) else {}
    actual_description = _safe_scalar(tool_input.get("description"))
    actual_subagent_type = _safe_scalar(tool_input.get("subagent_type"))
    description_matches = []
    for name, teammate in teammates.items():
        dispatch = teammate.get("agent_dispatch") if isinstance(teammate, dict) else None
        dispatch = dispatch if isinstance(dispatch, dict) else {}
        if actual_description and _safe_scalar(dispatch.get("description")) == actual_description:
            description_matches.append(str(name))
    prefix_matches = []
    if actual_subagent_type:
        prefix_matches = [str(name) for name in teammates.keys() if actual_subagent_type.startswith(str(name))]
    canonical_subagent_type = None
    if len(description_matches) == 1:
        canonical_subagent_type = description_matches[0]
    elif len(prefix_matches) == 1:
        canonical_subagent_type = prefix_matches[0]
    return {
        "reason_codes": [str(reason) for reason in reasons],
        "actual_input_keys": _safe_sorted_keys(tool_input),
        "actual_subagent_type": actual_subagent_type,
        "canonical_subagent_type": canonical_subagent_type,
        "actual_description": actual_description,
        "actual_model": _safe_scalar(tool_input.get("model")),
        "allowed_subagent_types": [str(item) for item in contract.get("allowed_agent_subagent_types", [])],
        "contract_teammate_keys": sorted(str(key) for key in teammates.keys()),
        "description_matches": sorted(description_matches),
        "binding": {key: _safe_scalar(value) for key, value in binding.items()},
    }


def _safe_sorted_keys(value: dict) -> list[str]:
    return sorted(str(key) for key in value.keys()) if isinstance(value, dict) else []


def _safe_scalar(value: object, limit: int = 160) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _generic_agent_model_alias(tool_input: dict) -> str | None:
    model = tool_input.get("model")
    if not isinstance(model, str):
        return None
    normalized = model.strip().casefold()
    return normalized if normalized in GENERIC_AGENT_MODEL_ALIASES else None


def _emit_agent_model_guard_denial(
    payload: dict,
    tool_input: dict,
    model_alias: str,
    reason: str,
    contract_guard: dict | None = None,
) -> None:
    if "outside a bridge window" in reason:
        guard_reason = "direct agent outside bridge window"
    elif (
        "agent_dispatch_subagent_type_not_in_contract" in reason
        or "agent_dispatch_input_keys_mismatch" in reason
        or "agent_dispatch_binding_" in reason
        or "dispatch_contract_" in reason
    ):
        guard_reason = "dispatch contract mismatch"
    elif "agent_dispatch_model_alias_forbidden" in reason:
        guard_reason = "generic model alias"
    elif "agent_dispatch_model_override_forbidden" in reason:
        guard_reason = "bridge teammate model override"
    elif model_alias in GENERIC_AGENT_MODEL_ALIASES:
        guard_reason = "generic model alias"
    else:
        guard_reason = "bridge teammate model override"
    guard = {
        "decision": "deny",
        "reason": guard_reason,
        "model": model_alias,
    }
    extra = {"model_guard": guard}
    if contract_guard:
        extra["dispatch_contract_guard"] = contract_guard
    _emit_tool_event(payload, tool_input, "Agent", status="denied", extra=extra)
    timestamp = now_iso()
    binding = observer_binding(payload, tool_input)
    record = {
        "timestamp": timestamp,
        **binding,
        "event_type": "agent_model_guard_denied",
        "tool_name": "Agent",
        "tool_use_id": payload.get("tool_use_id") or tool_input.get("tool_use_id"),
        "model": model_alias,
        "message_preview": reason,
        "cwd": payload.get("cwd") or tool_input.get("cwd"),
        "project_root": payload.get("project_root") or tool_input.get("project_root"),
    }
    if contract_guard:
        record["dispatch_contract_guard"] = contract_guard
    emit_observer_record("session_events", record)


def _emit_tool_event(payload: dict, tool_input: dict, tool_name: str, *, status: str, extra: dict | None = None) -> None:
    timestamp = now_iso()
    binding = observer_binding(payload, tool_input)
    tool_use_id = payload.get("tool_use_id") or tool_input.get("tool_use_id")
    soft_reminders = bash_execution_soft_reminders(tool_name, tool_input, binding, after=False)
    record = {
        "timestamp": timestamp,
        **binding,
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "action": _action_for_tool(tool_name),
        "target": compact_tool_target(tool_name, tool_input),
        "summary": compact_tool_summary(tool_name, tool_input),
        "status": status,
        "started_at": timestamp,
        "completed_at": None if status == "started" else timestamp,
        "duration_ms": None,
        "normalized_input": normalized_tool_input(tool_name, tool_input),
        "safe_input_preview": safe_input_preview(tool_input),
        "file_refs": tool_file_refs(tool_name, tool_input, after=False),
        "output_summary": None,
        "soft_reminders": soft_reminders,
        **tool_detail_fields(tool_name, tool_input, after=False),
    }
    if extra:
        record.update(extra)
    emit_observer_record("tool_events", record)
    session_event_type = "tool_call_started" if status == "started" else f"tool_call_{status}"
    emit_observer_record(
        "session_events",
        {
            "timestamp": timestamp,
            **binding,
            "event_type": session_event_type,
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "message_preview": compact_tool_summary(tool_name, tool_input),
            "cwd": payload.get("cwd") or tool_input.get("cwd"),
            "project_root": payload.get("project_root") or tool_input.get("project_root"),
        },
    )
    for reminder in soft_reminders:
        emit_observer_record(
            "session_events",
            {
                "timestamp": timestamp,
                **binding,
                "event_type": "soft_tool_reminder",
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "message_preview": reminder.get("message"),
                "reminder": reminder,
                "cwd": payload.get("cwd") or tool_input.get("cwd"),
                "project_root": payload.get("project_root") or tool_input.get("project_root"),
            },
        )


def _action_for_tool(tool_name: str) -> str:
    return {
        "Read": "read_file",
        "Grep": "search_text",
        "Glob": "match_files",
        "LS": "list_directory",
        "Edit": "edit_file",
        "Write": "write_file",
        "MultiEdit": "edit_file",
        "Bash": "run_command",
    }.get(tool_name, "tool_call")


if __name__ == "__main__":
    raise SystemExit(main())
