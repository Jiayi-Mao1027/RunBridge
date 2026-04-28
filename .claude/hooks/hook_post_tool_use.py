from __future__ import annotations

from common import detect_run_id, invoke_runtime_event, now_iso, read_hook_input, simple_block


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
    run_id = detect_run_id(payload)
    if not run_id:
        return 0

    tool_name = str(payload.get("tool_name") or "").strip()
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    tool_response = payload.get("tool_response") if isinstance(payload.get("tool_response"), dict) else {}
    status = str(payload.get("status") or tool_response.get("status") or "").lower()
    error = payload.get("error") or tool_response.get("error") or tool_response.get("error_or_null")
    failed = bool(error) or status in {"error", "failed", "failure"}

    packet = tool_input.get("packet") if isinstance(tool_input.get("packet"), dict) else {}
    binding = packet.get("binding", {}) if isinstance(packet, dict) else {}
    event_base = {
        "run_id": run_id,
        "main_session_id": payload.get("main_session_id") or payload.get("session_id") or tool_input.get("main_session_id"),
        "sub_session_id": payload.get("sub_session_id") or tool_input.get("sub_session_id") or binding.get("sub_session_id"),
        "bridge_window_id": payload.get("bridge_window_id") or tool_input.get("bridge_window_id") or binding.get("bridge_window_id"),
        "team_id": payload.get("team_id") or tool_input.get("team_id") or tool_response.get("team_id"),
        "task_id": payload.get("task_id") or tool_input.get("task_id") or tool_response.get("task_id"),
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
    else:
        return 0

    code, result, stderr = invoke_runtime_event(event, persist=True)
    if code != 0:
        return simple_block(f"PostToolUse blocked: runtime invocation failed. {stderr or result!r}")
    workflow = result.get("workflow_result", {})
    if not workflow.get("ok", False):
        check = workflow.get("check_result", {})
        return simple_block(f"PostToolUse blocked: {check.get('decision')} {check.get('reasons')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
