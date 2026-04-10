#!/usr/bin/env python3
from __future__ import annotations

from common import (
    CHECKPOINT_PATH,
    active_task_ids,
    append_event,
    latest_completed_task_ids,
    load_hook_input,
    now_iso,
    owned_process_snapshot,
    project_root,
    recent_events,
    write_json,
)


def main() -> int:
    payload = load_hook_input()
    checkpoint = {
        "checkpoint_time": now_iso(),
        "hook_payload": payload,
        "project_root": str(project_root(payload)),
        "open_tasks": active_task_ids(payload),
        "last_completed_tasks": latest_completed_task_ids(payload),
        "owned_process_snapshot": owned_process_snapshot(),
        "recent_events": recent_events(10),
    }
    write_json(CHECKPOINT_PATH, checkpoint)
    append_event(
        payload,
        "session_end",
        checkpoint_path=str(CHECKPOINT_PATH),
        open_task_count=len(checkpoint["open_tasks"]),
    )
    print(str(CHECKPOINT_PATH))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[session_end_checkpoint] non-fatal error: {exc}", file=sys.stderr)
        raise SystemExit(0)
