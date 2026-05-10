from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loader import ControlPaths
from persist import append_jsonl, atomic_write_json, sanitize_json_value
from repo_runtime import repo_key_for_paths


TRAJECTORY_PREVIEW_LIMIT = 1200


def record_workflow_trajectory_step(paths: ControlPaths, event: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    run_id = str(getattr(event, "run_id", None) or snapshot.get("run_id") or "")
    if not run_id:
        return {}
    payload = getattr(event, "payload", {}) if isinstance(getattr(event, "payload", {}), dict) else {}
    step = _base_step(paths, run_id, snapshot)
    step.update(
        {
            "phase": snapshot.get("current_phase"),
            "bridge_window_id": getattr(event, "bridge_window_id", None),
            "team_id": getattr(event, "team_id", None),
            "task_id": getattr(event, "task_id", None),
            "actor": {
                "session_id": getattr(event, "sub_session_id", None) or getattr(event, "main_session_id", None),
                "agent_type": getattr(event, "agent_type", None),
                "teammate_id": payload.get("teammate_id"),
            },
            "intent": {
                "local_goal": _workflow_goal(str(getattr(event, "event_kind", ""))),
                "contract_item_refs": _contract_refs(payload),
            },
            "action": {
                "kind": "workflow_event",
                "tool_name": getattr(event, "tool_name", None),
                "safe_input_preview": _redact_text(str(getattr(event, "event_kind", "")))[:TRAJECTORY_PREVIEW_LIMIT],
                "file_refs": [],
            },
            "observation": {
                "status": "recorded",
                "exit_code": None,
                "stdout_tail": None,
                "stderr_tail": None,
                "artifact_refs": payload.get("artifact_refs", []),
                "process_refs": payload.get("owned_process_refs", []),
            },
            "state_delta": _workflow_state_delta(paths.run_root(run_id), str(getattr(event, "event_kind", "")), payload),
            "related_completion_check_refs": _completion_check_refs(str(getattr(event, "event_kind", "")), getattr(event, "event_id", None)),
            "related_artifact_refs": payload.get("artifact_refs", []),
            "raw_refs": {
                "tool_event_ref": None,
                "sdk_stream_ref": None,
                "ledger_ref": f"event_log.jsonl:{getattr(event, 'event_id', '')}",
            },
        }
    )
    return append_trajectory_step(paths.run_root(run_id), step)


def record_tool_trajectory_step(run_root: str | Path, record: dict[str, Any]) -> dict[str, Any]:
    run_root_path = Path(run_root)
    run_id = str(record.get("run_id") or run_root_path.name)
    repo_key = str(record.get("repo_key") or _repo_key_from_run_root(run_root_path))
    step = {
        "trajectory_id": "",
        "step_index": 0,
        "timestamp": record.get("timestamp") or _now_iso(),
        "repo_key": repo_key,
        "run_id": run_id,
        "phase": record.get("phase"),
        "bridge_window_id": record.get("bridge_window_id"),
        "team_id": record.get("team_id"),
        "task_id": record.get("task_id"),
        "actor": {
            "session_id": record.get("session_id"),
            "agent_type": record.get("agent_type"),
            "teammate_id": record.get("teammate_id"),
        },
        "intent": {
            "local_goal": record.get("summary") or f"{record.get('tool_name') or 'tool'} use",
            "contract_item_refs": [],
        },
        "action": {
            "kind": "tool_use",
            "tool_name": record.get("tool_name"),
            "safe_input_preview": _compact_preview(record.get("safe_input_preview") or record.get("command_preview") or record.get("summary")),
            "file_refs": record.get("file_refs", []),
        },
        "observation": {
            "status": record.get("status"),
            "exit_code": record.get("exit_code"),
            "stdout_tail": _redact_text(str(record.get("stdout_tail") or ""))[:TRAJECTORY_PREVIEW_LIMIT] or None,
            "stderr_tail": _redact_text(str(record.get("stderr_tail") or ""))[:TRAJECTORY_PREVIEW_LIMIT] or None,
            "artifact_refs": record.get("artifact_refs", []) if isinstance(record.get("artifact_refs"), list) else [],
            "process_refs": record.get("spawned_processes", []),
        },
        "state_delta": {
            "opened_process_refs": record.get("spawned_processes", []),
            "completed_checklist_items": [],
            "new_blockers": _blockers_from_tool_record(record),
        },
        "raw_refs": {
            "tool_event_ref": f"tool_events.jsonl:{record.get('sequence') or record.get('monotonic_index')}",
            "sdk_stream_ref": None,
            "ledger_ref": None,
        },
    }
    return append_trajectory_step(run_root_path, step)


def record_guardrail_trajectory_step(paths: ControlPaths, run_id: str, validation: dict[str, Any], *, event_ref: str | None = None) -> dict[str, Any]:
    snapshot_path = paths.run_root(run_id) / "runtime_snapshot.json"
    snapshot = {}
    if snapshot_path.exists():
        try:
            loaded = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot = loaded if isinstance(loaded, dict) else {}
        except Exception:
            snapshot = {}
    step = _base_step(paths, run_id, snapshot)
    step.update(
        {
            "phase": snapshot.get("current_phase"),
            "bridge_window_id": validation.get("bridge_window_id"),
            "team_id": validation.get("team_id"),
            "task_id": validation.get("task_id"),
            "actor": {"session_id": None, "agent_type": "runtime", "teammate_id": None},
            "intent": {"local_goal": "validate structured output", "contract_item_refs": [str(validation.get("path") or "$")]},
            "action": {"kind": "guardrail_validation", "tool_name": None, "safe_input_preview": validation.get("error_type"), "file_refs": []},
            "observation": {
                "status": "valid" if validation.get("valid") else "failed",
                "exit_code": None,
                "stdout_tail": _redact_text(str(validation.get("message") or ""))[:TRAJECTORY_PREVIEW_LIMIT],
                "stderr_tail": None,
                "artifact_refs": [],
                "process_refs": [],
            },
            "state_delta": {"opened_process_refs": [], "completed_checklist_items": [], "new_blockers": [] if validation.get("valid") else [validation]},
            "supports_refs": [event_ref] if event_ref else [],
            "related_completion_check_refs": [event_ref] if event_ref else [],
            "raw_refs": {"tool_event_ref": None, "sdk_stream_ref": None, "ledger_ref": event_ref},
        }
    )
    return append_trajectory_step(paths.run_root(run_id), step)


def append_trajectory_step(run_root: str | Path, step: dict[str, Any]) -> dict[str, Any]:
    root = Path(run_root)
    path = root / "trajectory.jsonl"
    index_path = root / "trajectory_index.json"
    step_index = _next_step_index(path)
    trajectory_id = step.get("trajectory_id") or f"traj_{step_index:06d}"
    record = {
        **step,
        "trajectory_id": trajectory_id,
        "step_index": step_index,
        "timestamp": step.get("timestamp") or _now_iso(),
    }
    observation = record.get("observation") if isinstance(record.get("observation"), dict) else {}
    state_delta = record.get("state_delta") if isinstance(record.get("state_delta"), dict) else {}
    record["supports_refs"] = _unique_refs(
        record.get("supports_refs")
        or state_delta.get("supporting_trajectory_refs")
        or []
    )
    record["produces_refs"] = _unique_refs(record.get("produces_refs") or _produced_refs(observation))
    record["related_completion_check_refs"] = _unique_refs(record.get("related_completion_check_refs") or [])
    record["related_artifact_refs"] = _unique_refs(
        record.get("related_artifact_refs")
        or observation.get("artifact_refs")
        or []
    )
    record = sanitize_json_value(record)
    append_jsonl(path, record)
    index = _read_index(index_path)
    artifact_producers = index.get("artifact_producers") if isinstance(index.get("artifact_producers"), dict) else {}
    process_producers = index.get("process_producers") if isinstance(index.get("process_producers"), dict) else {}
    completion_checks = index.get("completion_checks") if isinstance(index.get("completion_checks"), list) else []
    observation = record.get("observation") if isinstance(record.get("observation"), dict) else {}
    state_delta = record.get("state_delta") if isinstance(record.get("state_delta"), dict) else {}
    for artifact_ref in observation.get("artifact_refs", []) if isinstance(observation.get("artifact_refs"), list) else []:
        artifact_producers[str(artifact_ref)] = record.get("trajectory_id")
    for process_ref in observation.get("process_refs", []) if isinstance(observation.get("process_refs"), list) else []:
        key = _process_ref_key(process_ref)
        if key:
            process_producers[key] = record.get("trajectory_id")
    action = record.get("action") if isinstance(record.get("action"), dict) else {}
    if action.get("kind") in {"workflow_event", "guardrail_validation"} and state_delta.get("supporting_trajectory_refs") is not None:
        completion_checks.append(
            {
                "trajectory_id": record.get("trajectory_id"),
                "step_index": step_index,
                "supporting_trajectory_refs": state_delta.get("supporting_trajectory_refs", []),
                "supports_refs": record.get("supports_refs", []),
                "related_completion_check_refs": record.get("related_completion_check_refs", []),
                "related_artifact_refs": record.get("related_artifact_refs", []),
            }
        )
        completion_checks = completion_checks[-100:]
    index.update(
        {
            "schema_version": "0.1.0",
            "updated_at": _now_iso(),
            "repo_key": record.get("repo_key"),
            "run_id": record.get("run_id"),
            "step_count": step_index,
            "latest_step": {
                "trajectory_id": record.get("trajectory_id"),
                "step_index": step_index,
                "timestamp": record.get("timestamp"),
                "action_kind": (record.get("action") or {}).get("kind") if isinstance(record.get("action"), dict) else None,
            },
            "artifact_producers": artifact_producers,
            "process_producers": process_producers,
            "completion_checks": completion_checks,
        }
    )
    atomic_write_json(index_path, index)
    return {"trajectory_ref": f"trajectory.jsonl:{step_index}", "trajectory_index": str(index_path)}


def _base_step(paths: ControlPaths, run_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "trajectory_id": "",
        "step_index": 0,
        "timestamp": _now_iso(),
        "repo_key": repo_key_for_paths(paths.control_root, paths.runtime_runs_root),
        "run_id": run_id,
        "phase": snapshot.get("current_phase"),
    }


def _workflow_goal(event_kind: str) -> str:
    return {
        "semantic_frozen": "freeze user intent semantics",
        "bridge_call_intended": "record bridge call intent",
        "pretooluse_allowed_by_main_leader": "precheck bridge call",
        "bridge_window_opened": "open bridge window",
        "team_create_succeeded": "create bridge team",
        "taskcreated_hook_accepted": "bind task to team",
        "message_dispatch_succeeded": "dispatch teammate messages",
        "team_idle_waiting": "record bridge team waiting state",
        "completion_contract_satisfied": "validate completion contract",
        "completion_contract_rejected": "record completion validation failure",
        "bridge_result_returned": "return bridge result",
        "orphan_timeout_without_bridge_return": "mark bridge window orphaned",
    }.get(event_kind, f"record {event_kind}")


def _workflow_state_delta(run_root: Path, event_kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    delta = {
        "opened_process_refs": payload.get("owned_process_refs", []) if event_kind == "team_idle_waiting" else [],
        "completed_checklist_items": payload.get("completed_checklist_items", []),
        "new_blockers": payload.get("missing_contract_items", []) if event_kind == "completion_contract_rejected" else [],
    }
    if event_kind in {"completion_contract_satisfied", "completion_contract_rejected", "bridge_result_returned"}:
        delta["supporting_trajectory_refs"] = _supporting_trajectory_refs(run_root, payload)
    return delta


def _contract_refs(payload: dict[str, Any]) -> list[str]:
    refs = []
    contract = payload.get("completion_contract") if isinstance(payload.get("completion_contract"), dict) else {}
    for key in ("required_outputs", "required_artifacts", "validation_requirements"):
        for item in contract.get(key, []) if isinstance(contract.get(key), list) else []:
            refs.append(f"completion.{key}.{item}")
    return refs[:20]


def _supporting_trajectory_refs(run_root: Path, payload: dict[str, Any]) -> list[str]:
    index = _read_index(run_root / "trajectory_index.json")
    refs: list[str] = []
    artifact_producers = index.get("artifact_producers") if isinstance(index.get("artifact_producers"), dict) else {}
    for artifact_ref in payload.get("artifact_refs", []) if isinstance(payload.get("artifact_refs"), list) else []:
        producer = artifact_producers.get(str(artifact_ref))
        if producer:
            refs.append(str(producer))
    latest = index.get("latest_step") if isinstance(index.get("latest_step"), dict) else {}
    if latest.get("trajectory_id"):
        refs.append(str(latest["trajectory_id"]))
    return _dedupe(refs)[:20]


def _completion_check_refs(event_kind: str, event_id: Any) -> list[str]:
    if event_kind not in {"completion_contract_satisfied", "completion_contract_rejected"}:
        return []
    if not event_id:
        return []
    return [f"completion_checks.jsonl:{event_id}"]


def _produced_refs(observation: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for artifact_ref in observation.get("artifact_refs", []) if isinstance(observation.get("artifact_refs"), list) else []:
        refs.append(f"artifact:{artifact_ref}")
    for process_ref in observation.get("process_refs", []) if isinstance(observation.get("process_refs"), list) else []:
        key = _process_ref_key(process_ref)
        if key:
            refs.append(f"process:{key}")
    return refs


def _unique_refs(values: Any) -> list[str]:
    if not isinstance(values, list):
        values = [values] if values else []
    refs: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value)
        if text:
            refs.append(text)
    return _dedupe(refs)[:50]


def _process_ref_key(process_ref: Any) -> str | None:
    if isinstance(process_ref, dict):
        for key in ("process_ref", "process_id", "pid", "log_path"):
            if process_ref.get(key):
                return str(process_ref.get(key))
    if process_ref:
        return str(process_ref)
    return None


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _blockers_from_tool_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    if str(record.get("status") or "").lower() not in {"failed", "error"}:
        return []
    return [{"tool_name": record.get("tool_name"), "error_or_null": record.get("error_or_null")}]


def _compact_preview(value: Any) -> str:
    if isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    else:
        text = str(value or "")
    return _redact_text(text)[:TRAJECTORY_PREVIEW_LIMIT]


def _repo_key_from_run_root(run_root: Path) -> str:
    parts = list(run_root.resolve().parts)
    for index, part in enumerate(parts):
        if part == "projects" and index + 1 < len(parts):
            return parts[index + 1]
    return "unscoped_repo"


def _read_index(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _next_step_index(path: Path) -> int:
    if not path.exists():
        return 1
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) + 1
    except Exception:
        return 1


def _redact_text(text: str) -> str:
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)(\s*[:=]\s*)(\S+)", r"\1\2<redacted>", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-<redacted>", text)
    return text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
