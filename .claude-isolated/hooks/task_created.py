#!/usr/bin/env python3
from __future__ import annotations

import hashlib

from common import (
    append_event,
    block,
    envelope_path,
    infer_layer,
    load_hook_input,
    now_iso,
    parse_embedded_json,
    read_json,
    write_json,
)


def fallback_task_id(subject: str, description: str) -> str:
    basis = f"{subject}\n{description}".strip() or now_iso()
    return f"task-{hashlib.sha1(basis.encode('utf-8')).hexdigest()[:12]}"


def main() -> int:
    payload = load_hook_input()
    subject = str(payload.get("task_subject") or payload.get("subject") or "").strip()
    description = str(payload.get("task_description") or payload.get("description") or "").strip()
    embedded = parse_embedded_json(description)

    task_id = str(payload.get("task_id") or embedded.get("task_id") or fallback_task_id(subject, description))
    team_name = str(payload.get("team_name") or embedded.get("team_name") or "").strip()
    teammate_name = str(payload.get("teammate_name") or embedded.get("teammate_name") or "").strip()
    task_kind = str(payload.get("task_kind") or embedded.get("task_kind") or "").strip()
    layer = str(payload.get("layer") or embedded.get("layer") or infer_layer(team_name, teammate_name, task_kind))

    if not subject and not description:
        return block("TaskCreated blocked: task is missing both subject and description.")

    envelope = {
        "protocol_version": "1.0",
        "task_id": task_id,
        "created_at": now_iso(),
        "layer": layer,
        "team_name": team_name,
        "teammate_name": teammate_name,
        "task_kind": task_kind or "unspecified",
        "subject": subject,
        "objective": str(embedded.get("objective") or description).strip(),
        "inputs": embedded.get("inputs", []),
        "required_outputs": embedded.get("required_outputs", []),
        "completion_contract": embedded.get("completion_contract", {}),
        "raw_hook_payload": payload,
    }

    path = envelope_path(payload, task_id)
    existing = read_json(path, {})
    if existing:
        existing_subject = str(existing.get("subject") or "").strip()
        if existing_subject and subject and existing_subject != subject:
            return block(
                f'TaskCreated blocked: task_id "{task_id}" already exists with a different subject.'
            )

    write_json(path, envelope)
    append_event(
        payload,
        "task_created",
        task_id=task_id,
        layer=layer,
        team_name=team_name,
        teammate_name=teammate_name,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
