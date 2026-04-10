#!/usr/bin/env python3
from __future__ import annotations

from common import (
    append_event,
    block,
    load_hook_input,
    load_receipt,
    now_iso,
    parse_embedded_json,
    status_path,
    write_json,
)


def main() -> int:
    payload = load_hook_input()
    description = str(payload.get("task_description") or payload.get("description") or "").strip()
    embedded = parse_embedded_json(description)

    task_id = str(payload.get("task_id") or embedded.get("task_id") or "no-task")
    team_name = str(payload.get("team_name") or embedded.get("team_name") or "").strip()
    teammate_name = str(payload.get("teammate_name") or embedded.get("teammate_name") or "").strip()
    result_kind = str(payload.get("result_kind") or embedded.get("result_kind") or "partial").strip()
    produced_artifacts = embedded.get("produced_artifacts", [])
    open_issues = embedded.get("open_issues", [])
    handoff_targets = embedded.get("handoff_targets", [])
    resume_hint = str(payload.get("resume_hint") or embedded.get("resume_hint") or "").strip()

    if result_kind in {"final", "completed"} and not load_receipt(payload, task_id):
        return block(
            f'TeammateIdle blocked: task "{task_id}" claims a final result but has no completion receipt.'
        )

    if open_issues and not resume_hint:
        return block(
            f'TeammateIdle blocked: task "{task_id}" has open issues but no resume hint for follow-up.'
        )

    status = {
        "protocol_version": "1.0",
        "task_id": task_id,
        "team_name": team_name,
        "teammate_name": teammate_name,
        "status": "ready_for_idle",
        "result_kind": result_kind,
        "produced_artifacts": produced_artifacts,
        "open_issues": open_issues,
        "handoff_targets": handoff_targets,
        "resume_hint": resume_hint,
        "written_at": now_iso(),
        "raw_hook_payload": payload,
    }
    write_json(status_path(payload, task_id, teammate_name), status)
    append_event(
        payload,
        "teammate_idle",
        task_id=task_id,
        team_name=team_name,
        teammate_name=teammate_name,
        result_kind=result_kind,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
