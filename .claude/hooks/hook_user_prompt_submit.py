from __future__ import annotations

from common import detect_run_id, emit_observer_record, invoke_runtime_event, is_bridge_child_session, now_iso, observer_binding, read_hook_input, redact_observer_text


def main() -> int:
    payload = read_hook_input()
    timestamp = now_iso()
    binding = observer_binding(payload)
    prompt = payload.get("prompt") or payload.get("user_input")
    emit_observer_record(
        "session_events",
        {
            "timestamp": timestamp,
            **binding,
            "event_type": "user_prompt",
            "message_preview": redact_observer_text(str(prompt or ""))[:500],
            "cwd": payload.get("cwd"),
            "project_root": payload.get("project_root"),
        },
    )
    if is_bridge_child_session():
        return 0

    run_id = detect_run_id(payload)
    if not run_id:
        return 0

    event = {
        "run_id": run_id,
        "main_session_id": payload.get("main_session_id") or payload.get("session_id"),
        "agent_id": "hook.user_prompt_submit",
        "agent_type": "hook",
        "event_kind": "user_prompt_submitted",
        "timestamp": timestamp,
        "payload": {
            "user_input": payload.get("prompt") or payload.get("user_input"),
            "semantic_refresh_requested": True,
        },
    }
    invoke_runtime_event(event, persist=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
