from __future__ import annotations

from common import detect_run_id, invoke_runtime_event, is_bridge_child_session, now_iso, read_hook_input


def main() -> int:
    if is_bridge_child_session():
        return 0

    payload = read_hook_input()
    run_id = detect_run_id(payload)
    if not run_id:
        return 0

    event = {
        "run_id": run_id,
        "main_session_id": payload.get("main_session_id") or payload.get("session_id"),
        "agent_id": "hook.user_prompt_submit",
        "agent_type": "hook",
        "event_kind": "user_prompt_submitted",
        "timestamp": now_iso(),
        "payload": {
            "user_input": payload.get("prompt") or payload.get("user_input"),
            "semantic_refresh_requested": True,
        },
    }
    invoke_runtime_event(event, persist=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
