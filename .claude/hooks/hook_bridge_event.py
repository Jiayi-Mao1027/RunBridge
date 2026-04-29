from __future__ import annotations

from common import detect_run_id, invoke_runtime_event, now_iso, read_hook_input, simple_block


HOOK_TO_EVENT = {
    "BridgeWindowOpened": "bridge_window_opened",
    "BridgePacketAccepted": "bridge_packet_accepted",
    "BridgePacketRejected": "bridge_packet_rejected",
}


def main() -> int:
    payload = read_hook_input()
    hook_name = str(payload.get("hook_event_name") or payload.get("hook_name") or "").strip()
    event_kind = str(payload.get("event_kind") or HOOK_TO_EVENT.get(hook_name) or "").strip()
    if event_kind not in set(HOOK_TO_EVENT.values()):
        return simple_block("Bridge event blocked: missing or unsupported event_kind.")
    run_id = detect_run_id(payload)
    if not run_id:
        return simple_block("Bridge event blocked: runtime run binding missing; SessionStart active-run is required.")

    packet = payload.get("packet") if isinstance(payload.get("packet"), dict) else {}
    binding = packet.get("binding", {}) if isinstance(packet, dict) else {}
    event = {
        "run_id": run_id,
        "main_session_id": payload.get("main_session_id") or payload.get("session_id") or binding.get("main_session_id"),
        "sub_session_id": payload.get("sub_session_id") or binding.get("sub_session_id"),
        "bridge_window_id": payload.get("bridge_window_id") or binding.get("bridge_window_id"),
        "team_id": payload.get("team_id"),
        "task_id": payload.get("task_id"),
        "agent_id": payload.get("agent_id") or "bridge-leader",
        "agent_type": payload.get("agent_type") or "bridge-leader",
        "event_kind": event_kind,
        "timestamp": now_iso(),
        "payload": {
            "packet": packet,
            "reasons": payload.get("reasons", []),
            "error_or_null": payload.get("error_or_null"),
        },
    }

    code, result, stderr = invoke_runtime_event(event, persist=True)
    if code != 0:
        return simple_block(f"Bridge event blocked: runtime invocation failed. {stderr or result!r}")
    workflow = result.get("workflow_result", {})
    if not workflow.get("ok", False):
        check = workflow.get("check_result", {})
        return simple_block(f"Bridge event blocked: {check.get('decision')} {check.get('reasons')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
