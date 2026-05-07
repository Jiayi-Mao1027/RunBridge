from __future__ import annotations

from common import (
    compact_tool_summary,
    compact_tool_target,
    control_binding_value,
    control_main_session_id,
    detect_run_id,
    emit_observer_record,
    invoke_runtime_event,
    load_last_bridge_packet,
    now_iso,
    normalized_tool_input,
    observer_binding,
    observer_tool_start_record,
    output_summary,
    read_hook_input,
    safe_input_preview,
    simple_block,
    tool_detail_fields,
    tool_file_refs,
    duration_ms,
)


BRIDGE_TOOL_NAMES = {"call_bridge_sdk", "mcp__bridge__call_bridge_sdk"}
SUCCESS_BY_TOOL = {
    "team_create": "team_create_succeeded",
    "task_create": "task_create_succeeded",
    "send_messages": "message_dispatch_succeeded",
    "send_message": "message_dispatch_succeeded",
    "team_delete": "team_delete_succeeded",
}
FAILURE_BY_TOOL = {
    "team_create": "team_create_failed",
    "task_create": "task_create_failed",
    "send_messages": "message_dispatch_failed",
    "send_message": "message_dispatch_failed",
    "team_delete": "team_delete_failed",
}


def main() -> int:
    payload = read_hook_input()
    tool_name = str(payload.get("tool_name") or "").strip()
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    tool_response = payload.get("tool_response") if isinstance(payload.get("tool_response"), dict) else {}
    status = str(payload.get("status") or tool_response.get("status") or "").lower()
    error = payload.get("error") or tool_response.get("error") or tool_response.get("error_or_null")
    failed = bool(error) or status in {"error", "failed", "failure"}
    if tool_name:
        _emit_tool_event(payload, tool_input, tool_response, tool_name, failed=failed, error=error)
    if tool_name not in BRIDGE_TOOL_NAMES and tool_name not in SUCCESS_BY_TOOL:
        return 0

    if tool_name == "mcp__bridge__call_bridge_sdk":
        # The MCP bridge server records the bridge-window return from inside
        # the bridge runtime. This hook must not block the SDK result if the
        # host hook payload cannot be rebound to SessionStart state.
        return 0

    run_id = detect_run_id(payload)
    if not run_id:
        return simple_block("PostToolUse blocked: runtime run binding missing; SessionStart active-run is required.")

    tool_arguments = tool_input.get("arguments") if isinstance(tool_input.get("arguments"), dict) else {}

    packet = tool_input.get("packet") if isinstance(tool_input.get("packet"), dict) else {}
    if not packet and isinstance(tool_arguments.get("packet"), dict):
        packet = tool_arguments["packet"]
    if not packet and tool_name in BRIDGE_TOOL_NAMES:
        packet = load_last_bridge_packet()
    binding = packet.get("binding", {}) if isinstance(packet, dict) else {}
    event_base = {
        "run_id": run_id,
        "main_session_id": control_main_session_id(payload, tool_input, packet),
        "sub_session_id": payload.get("sub_session_id") or tool_input.get("sub_session_id") or binding.get("sub_session_id") or control_binding_value("sub_session_id", payload, tool_input, packet),
        "bridge_window_id": payload.get("bridge_window_id") or tool_input.get("bridge_window_id") or binding.get("bridge_window_id") or control_binding_value("bridge_window_id", payload, tool_input, packet),
        "team_id": payload.get("team_id") or tool_input.get("team_id") or tool_response.get("team_id") or control_binding_value("team_id", payload, tool_input, packet),
        "task_id": payload.get("task_id") or tool_input.get("task_id") or tool_response.get("task_id") or control_binding_value("task_id", payload, tool_input, packet),
        "agent_id": payload.get("agent_id") or tool_input.get("agent_id") or "bridge-leader",
        "agent_type": payload.get("agent_type") or tool_input.get("agent_type") or "bridge-leader",
        "tool_name": tool_name,
        "tool_use_id": payload.get("tool_use_id") or tool_input.get("tool_use_id"),
        "timestamp": now_iso(),
    }

    if tool_name in BRIDGE_TOOL_NAMES:
        event_kind = "call_bridge_sdk_error" if failed else "bridge_result_returned"
        bridge_result = tool_response.get("bridge_result") if isinstance(tool_response.get("bridge_result"), dict) else tool_response
        event = {
            **event_base,
            "agent_type": payload.get("agent_type") or "main-leader",
            "event_kind": event_kind,
            "payload": {
                "tool_input": tool_input,
                "tool_response": tool_response,
                "bridge_result": bridge_result,
                "error_or_null": error,
            },
        }
    elif tool_name in SUCCESS_BY_TOOL:
        event = {
            **event_base,
            "event_kind": FAILURE_BY_TOOL[tool_name] if failed else SUCCESS_BY_TOOL[tool_name],
            "payload": {
                "tool_input": tool_input,
                "tool_response": tool_response,
                "error_or_null": error,
                "team_name": tool_response.get("team_name") or tool_input.get("team_name"),
                "teammate_ids": tool_response.get("teammate_ids") or tool_input.get("teammate_ids") or [],
            },
        }
    code, result, stderr = invoke_runtime_event(event, persist=True)
    if code != 0:
        return simple_block(f"PostToolUse blocked: runtime invocation failed. {stderr or result!r}")
    workflow = result.get("workflow_result", {})
    if not workflow.get("ok", False):
        check = workflow.get("check_result", {})
        return simple_block(f"PostToolUse blocked: {check.get('decision')} {check.get('reasons')}")
    return 0


def _emit_tool_event(payload: dict, tool_input: dict, tool_response: dict, tool_name: str, *, failed: bool, error: object) -> None:
    binding = observer_binding(payload, tool_input)
    run_id = binding.get("run_id") if isinstance(binding.get("run_id"), str) else ""
    tool_use_id = payload.get("tool_use_id") or tool_input.get("tool_use_id")
    completed_at = now_iso()
    started = observer_tool_start_record(run_id, str(tool_use_id) if tool_use_id else None)
    started_at = started.get("started_at") or started.get("timestamp")
    record = {
        "timestamp": completed_at,
        **binding,
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "action": _action_for_tool(tool_name),
        "target": compact_tool_target(tool_name, tool_input),
        "summary": compact_tool_summary(tool_name, tool_input),
        "status": "failed" if failed else "completed",
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_ms": duration_ms(started_at, completed_at),
        "normalized_input": normalized_tool_input(tool_name, tool_input),
        "safe_input_preview": safe_input_preview(tool_input),
        "file_refs": tool_file_refs(tool_name, tool_input, after=True),
        "output_summary": output_summary(tool_response, failed=failed),
        "exit_code": tool_response.get("exit_code"),
        "stdout_tail": _tail(tool_response.get("stdout") or tool_response.get("output")),
        "stderr_tail": _tail(tool_response.get("stderr")),
        "error_or_null": error,
        **tool_detail_fields(tool_name, tool_input, tool_response, failed=failed, after=True),
    }
    emit_observer_record("tool_events", record)
    emit_observer_record(
        "session_events",
        {
            "timestamp": completed_at,
            **binding,
            "event_type": "tool_call_failed" if failed else "tool_call_completed",
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "message_preview": compact_tool_summary(tool_name, tool_input),
            "cwd": payload.get("cwd") or tool_input.get("cwd"),
            "project_root": payload.get("project_root") or tool_input.get("project_root"),
            "parent_message_id": payload.get("parent_message_id"),
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


def _tail(value: object, limit: int = 1200) -> str | None:
    if not isinstance(value, str):
        return None
    return value[-limit:]


if __name__ == "__main__":
    raise SystemExit(main())
