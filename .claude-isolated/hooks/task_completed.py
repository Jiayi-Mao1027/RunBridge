#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from common import (
    append_event,
    block,
    envelope_path,
    load_envelope,
    load_hook_input,
    now_iso,
    parse_embedded_json,
    project_root,
    read_json,
    receipt_path,
    relative_to_project,
    resolve_path,
    write_json,
)


def validate_outputs(root: Path, outputs: list[str]) -> tuple[list[str], list[str]]:
    delivered: list[str] = []
    missing: list[str] = []
    for item in outputs:
        path = resolve_path(item, root)
        label = relative_to_project(path, root)
        if path.exists():
            delivered.append(label)
        else:
            missing.append(label)
    return delivered, missing


def main() -> int:
    payload = load_hook_input()
    description = str(payload.get("task_description") or payload.get("description") or "").strip()
    embedded = parse_embedded_json(description)
    task_id = str(payload.get("task_id") or embedded.get("task_id") or "no-task")
    envelope = load_envelope(payload, task_id)
    root = project_root(payload)

    required_outputs = envelope.get("required_outputs", []) if isinstance(envelope, dict) else []
    delivered_outputs = embedded.get("delivered_outputs", payload.get("delivered_outputs", required_outputs))
    delivered_outputs = delivered_outputs if isinstance(delivered_outputs, list) else []
    delivered, missing = validate_outputs(root, [str(item) for item in delivered_outputs])

    final_status = str(payload.get("final_status") or embedded.get("final_status") or "completed").strip()
    if final_status == "completed" and missing:
        return block(
            f'TaskCompleted blocked: task "{task_id}" is missing required outputs: {", ".join(missing)}'
        )

    receipt = {
        "protocol_version": "1.0",
        "task_id": task_id,
        "team_name": str(payload.get("team_name") or envelope.get("team_name") or "").strip(),
        "teammate_name": str(payload.get("teammate_name") or envelope.get("teammate_name") or "").strip(),
        "final_status": final_status,
        "delivered_outputs": delivered,
        "completion_checks": {
            "required_outputs_present": not missing,
            "schema_valid": True,
            "missing_outputs": missing,
        },
        "handoff_contract": embedded.get("handoff_contract", {}),
        "written_at": now_iso(),
        "raw_hook_payload": payload,
    }

    existing = read_json(receipt_path(payload, task_id), {})
    if existing and existing.get("final_status") == "completed" and final_status != "completed":
        return block(
            f'TaskCompleted blocked: task "{task_id}" already has a completed receipt and cannot be downgraded.'
        )

    write_json(receipt_path(payload, task_id), receipt)
    append_event(
        payload,
        "task_completed",
        task_id=task_id,
        final_status=final_status,
        delivered_outputs=delivered,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
