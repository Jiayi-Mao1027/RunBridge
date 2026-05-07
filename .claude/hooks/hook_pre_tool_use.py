from __future__ import annotations

from common import (
    compact_tool_summary,
    compact_tool_target,
    control_binding_value,
    control_main_session_id,
    detect_run_id,
    emit_companion_event,
    invoke_runtime_event,
    is_bridge_child_session,
    load_last_bridge_packet,
    now_iso,
    normalized_tool_input,
    pretool_deny,
    read_hook_input,
    safe_input_preview,
    tool_file_refs,
)


BRIDGE_TOOL_NAMES = {"call_bridge_sdk", "mcp__bridge__call_bridge_sdk"}
START_BY_TOOL = {
    "team_create": "team_create_started",
    "task_create": "task_create_started",
    "send_messages": "message_dispatch_started",
    "send_message": "message_dispatch_started",
    "task_complete": "artifacts_ready",
    "team_delete": "team_delete_started",
}


def main() -> int:
    payload = read_hook_input()
    tool_name = str(payload.get("tool_name") or "").strip()
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if is_bridge_child_session() and tool_name:
        _emit_child_tool_event(payload, tool_input, tool_name, status="started")
    if tool_name not in BRIDGE_TOOL_NAMES and tool_name not in START_BY_TOOL:
        return 0

    packet = tool_input.get("packet") if isinstance(tool_input, dict) else None
    tool_arguments = tool_input.get("arguments") if isinstance(tool_input.get("arguments"), dict) else {}
    if packet is None and isinstance(tool_arguments, dict):
        packet = tool_arguments.get("packet")
    if packet is None and tool_name in BRIDGE_TOOL_NAMES:
        packet = load_last_bridge_packet() or None

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


def _emit_child_tool_event(payload: dict, tool_input: dict, tool_name: str, *, status: str) -> None:
    timestamp = now_iso()
    emit_companion_event(
        "tool_events",
        {
            "timestamp": timestamp,
            "run_id": detect_run_id(payload) or "",
            "main_session_id": control_main_session_id(payload, tool_input),
            "sub_session_id": control_binding_value("sub_session_id", payload, tool_input),
            "bridge_window_id": control_binding_value("bridge_window_id", payload, tool_input),
            "team_id": control_binding_value("team_id", payload, tool_input),
            "task_id": control_binding_value("task_id", payload, tool_input),
            "teammate_id": payload.get("agent_id") or tool_input.get("agent_id"),
            "agent_id": payload.get("agent_id") or tool_input.get("agent_id"),
            "agent_type": payload.get("agent_type") or tool_input.get("agent_type"),
            "tool_name": tool_name,
            "tool_use_id": payload.get("tool_use_id") or tool_input.get("tool_use_id"),
            "action": _action_for_tool(tool_name),
            "target": compact_tool_target(tool_name, tool_input),
            "summary": compact_tool_summary(tool_name, tool_input),
            "status": status,
            "started_at": timestamp,
            "completed_at": None,
            "duration_ms": None,
            "normalized_input": normalized_tool_input(tool_name, tool_input),
            "safe_input_preview": safe_input_preview(tool_input),
            "file_refs": tool_file_refs(tool_name, tool_input, after=False),
            "output_summary": None,
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
