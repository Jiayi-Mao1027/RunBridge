from __future__ import annotations

from common import (
    detect_run_id,
    invoke_runtime,
    now_iso,
    parse_embedded_json,
    read_hook_input,
    simple_block,
)


def main() -> int:
    payload = read_hook_input()
    run_id = detect_run_id(payload)
    if not run_id:
        return simple_block("TaskCompleted blocked: missing run_id / CLAUDE_CONTROL_RUN_ID.")

    description = str(payload.get("task_description") or payload.get("description") or "").strip()
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        return simple_block("TaskCompleted blocked: missing task_id from hook payload.")

    embedded = parse_embedded_json(description)

    request = {
        "run_id": run_id,
        "action": "complete_task",
        "task_id": task_id,
        "payload": {
            "produced_artifacts": embedded.get("produced_artifacts", []),
            "artifact_refs": embedded.get("artifact_refs", []),
        },
        "reason": "hook TaskCompleted",
        "requester": "hook.task_completed",
        "timestamp": now_iso(),
        "trigger_source": "hook",
        "hook_name": "TaskCompleted",
        "event_name": payload.get("hook_event_name"),
        "request_id": task_id,
    }

    if "completion_checks" in embedded:
        request["payload"]["completion_checks"] = embedded["completion_checks"]

    code, result, stderr = invoke_runtime(request, persist=True)
    if code != 0:
        return simple_block(f"TaskCompleted blocked: runtime invocation failed. {stderr or result!r}")

    dispatch = result.get("dispatch_result", {})
    if not dispatch.get("ok", False):
        return simple_block(f"TaskCompleted blocked: {dispatch.get('decision')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
