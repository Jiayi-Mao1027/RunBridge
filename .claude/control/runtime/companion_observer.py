from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import re

from loader import ControlPaths
from persist import append_jsonl, sanitize_json_value


COMPANION_EVENT_KINDS = {
    "agent_messages",
    "tool_events",
    "teammate_reports",
    "bridge_packets",
    "artifacts",
    "completion_checks",
    "process_events",
}


def emit_companion_event(paths: ControlPaths, run_id: str, kind: str, payload: dict[str, Any]) -> None:
    if kind not in COMPANION_EVENT_KINDS:
        raise ValueError(f"unknown companion event kind: {kind}")
    run_root = paths.run_root(run_id)
    event_path = run_root / f"{kind}.jsonl"
    sequence = _next_sequence(event_path)
    record = {
        "timestamp": payload.get("timestamp") or _now_iso(),
        "event_type": kind,
        **payload,
        "sequence": payload.get("sequence") or sequence,
        "monotonic_index": payload.get("monotonic_index") or sequence,
    }
    append_jsonl(event_path, record)
    companion_path = run_root / "companion_events.jsonl"
    companion_sequence = _next_sequence(companion_path)
    append_jsonl(
        companion_path,
        {
            **record,
            "companion_sequence": companion_sequence,
            "source_kind": kind,
            "source_file": f"{kind}.jsonl",
            "source_sequence": record["sequence"],
            "source_offset": record["sequence"],
        },
    )


def emit_companion_event_to_run_root(run_root: str | Path, kind: str, payload: dict[str, Any]) -> None:
    if kind not in COMPANION_EVENT_KINDS:
        raise ValueError(f"unknown companion event kind: {kind}")
    root = Path(run_root).expanduser().resolve()
    event_path = root / f"{kind}.jsonl"
    sequence = _next_sequence(event_path)
    record = {
        "timestamp": payload.get("timestamp") or _now_iso(),
        "event_type": kind,
        **payload,
        "sequence": payload.get("sequence") or sequence,
        "monotonic_index": payload.get("monotonic_index") or sequence,
    }
    append_jsonl(event_path, record)
    companion_path = root / "companion_events.jsonl"
    companion_sequence = _next_sequence(companion_path)
    append_jsonl(
        companion_path,
        {
            **record,
            "companion_sequence": companion_sequence,
            "source_kind": kind,
            "source_file": f"{kind}.jsonl",
            "source_sequence": record["sequence"],
            "source_offset": record["sequence"],
        },
    )


def observe_workflow_event(paths: ControlPaths, event: Any, snapshot: dict[str, Any]) -> dict[str, str]:
    payload = event.payload if isinstance(getattr(event, "payload", None), dict) else {}
    base = _base(event)
    written: dict[str, str] = {}

    packet = payload.get("packet")
    if isinstance(packet, dict) and event.event_kind in {
        "bridge_call_intended",
        "bridge_window_opened",
        "bridge_packet_accepted",
        "taskcreated_hook_accepted",
    }:
        emit_companion_event(paths, event.run_id, "bridge_packets", {**base, **_packet_summary(packet)})
        written["bridge_packets"] = str(paths.run_root(event.run_id) / "bridge_packets.jsonl")

    if event.event_kind == "message_dispatch_succeeded":
        for message in _messages_from_payload(payload):
            emit_companion_event(paths, event.run_id, "agent_messages", {**base, **message})
        if _messages_from_payload(payload):
            written["agent_messages"] = str(paths.run_root(event.run_id) / "agent_messages.jsonl")

    tool_event = _tool_event_from_workflow(event, payload)
    if tool_event:
        emit_companion_event(paths, event.run_id, "tool_events", {**base, **tool_event})
        written["tool_events"] = str(paths.run_root(event.run_id) / "tool_events.jsonl")

    for report in _reports_from_event(event, payload):
        emit_companion_event(paths, event.run_id, "teammate_reports", {**base, **report})
    if _reports_from_event(event, payload):
        written["teammate_reports"] = str(paths.run_root(event.run_id) / "teammate_reports.jsonl")

    for artifact in _artifacts_from_event(event, payload):
        emit_companion_event(paths, event.run_id, "artifacts", {**base, **artifact})
    if _artifacts_from_event(event, payload):
        written["artifacts"] = str(paths.run_root(event.run_id) / "artifacts.jsonl")

    completion_check = _completion_check_from_event(event, payload)
    if completion_check:
        emit_companion_event(paths, event.run_id, "completion_checks", {**base, **completion_check})
        written["completion_checks"] = str(paths.run_root(event.run_id) / "completion_checks.jsonl")

    for process_event in _process_events_from_event(event, payload):
        emit_companion_event(paths, event.run_id, "process_events", {**base, **process_event})
    if _process_events_from_event(event, payload):
        written["process_events"] = str(paths.run_root(event.run_id) / "process_events.jsonl")

    return written


def _base(event: Any) -> dict[str, Any]:
    return {
        "timestamp": event.timestamp,
        "source_event_id": event.event_id,
        "source_event_kind": event.event_kind,
        "run_id": event.run_id,
        "main_session_id": event.main_session_id,
        "sub_session_id": event.sub_session_id,
        "bridge_window_id": event.bridge_window_id,
        "team_id": event.team_id,
        "task_id": event.task_id,
        "agent_id": event.agent_id,
        "agent_type": event.agent_type,
        "tool_name": event.tool_name,
        "tool_use_id": event.tool_use_id,
    }


def _packet_summary(packet: dict[str, Any]) -> dict[str, Any]:
    task = packet.get("task_spec") if isinstance(packet.get("task_spec"), dict) else {}
    team = packet.get("team_spec") if isinstance(packet.get("team_spec"), dict) else {}
    ownership = team.get("ownership_boundary") if isinstance(team.get("ownership_boundary"), dict) else {}
    teammates = []
    for teammate in team.get("teammate_specs", []) if isinstance(team.get("teammate_specs"), list) else []:
        if not isinstance(teammate, dict):
            continue
        teammates.append(
            {
                "agent_type": teammate.get("teammate_name") or teammate.get("role"),
                "role": teammate.get("role"),
                "allowed_tools": teammate.get("allowed_tools", []),
                "responsibilities": teammate.get("responsibilities", []),
            }
        )
    return {
        "target_phase": packet.get("target_phase"),
        "task_title": task.get("task_subject"),
        "original_user_instruction": task.get("original_user_instruction") or task.get("task_description"),
        "objective": task.get("task_description"),
        "instruction_coverage_checklist": task.get("instruction_coverage_checklist", []),
        "scope": {
            "readable_scopes": ownership.get("readable_scopes", []),
            "writable_scopes": ownership.get("writable_scopes", []),
            "forbidden_actions": ownership.get("forbidden_actions", []),
        },
        "team_spec": teammates,
        "completion_contract": packet.get("completion_contract", {}),
        "report_contract": packet.get("report_contract", {}),
    }


def _messages_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []
    packet = payload.get("packet") if isinstance(payload.get("packet"), dict) else {}
    coverage_refs = _coverage_refs(packet)
    result = []
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            continue
        body = str(message.get("body") or "")
        result.append(
            {
                "message_id": f"msg_{index}",
                "direction": "bridge_leader_to_teammate",
                "from": "bridge-leader",
                "to": message.get("teammate_id_or_null") or _first_line_role(body),
                "message_type": "assignment",
                "summary": _summarize(body),
                "payload_ref": "event_log.jsonl",
                "body_preview": body[:1200],
                "coverage_refs": coverage_refs,
                "requires_response": True,
            }
        )
    return result


def _tool_event_from_workflow(event: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
    if not event.tool_name:
        return None
    if event.event_kind.endswith("_started"):
        status = "started"
    elif event.event_kind.endswith("_succeeded"):
        status = "completed"
    elif event.event_kind.endswith("_failed"):
        status = "failed"
    else:
        return None
    return {
        "teammate_id": event.agent_id,
        "action": _action_for_tool(str(event.tool_name)),
        "target": _target_from_payload(payload),
        "summary": _summarize(payload),
        "status": status,
        "started_at": event.timestamp if status == "started" else None,
        "completed_at": event.timestamp if status in {"completed", "failed"} else None,
        "duration_ms": None,
        "normalized_input": _normalized_input(payload),
        "safe_input_preview": _safe_input_preview(payload),
        "file_refs": _file_refs(str(event.tool_name), payload),
        "output_summary": _output_summary(payload, status=status),
        "exit_code": payload.get("exit_code"),
        "error_or_null": payload.get("error_or_null"),
    }


def _reports_from_event(event: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
    reports = payload.get("reports")
    if not isinstance(reports, list):
        bridge_result = payload.get("bridge_result") if isinstance(payload.get("bridge_result"), dict) else {}
        reports = bridge_result.get("reports") if isinstance(bridge_result.get("reports"), list) else []
    result = []
    for report in reports:
        result.append(
            {
                "teammate_id": event.agent_id,
                "report_type": _report_type_for_event(event.event_kind),
                "progress_state": _progress_state_for_event(event.event_kind, report),
                "summary": _summarize(report),
                "report": report,
                "completed_items": _list_field(report, "completed_items"),
                "open_items": _list_field(report, "open_items"),
                "blocked_items": _list_field(report, "blocked_items"),
                "evidence_refs": _list_field(report, "evidence_refs"),
                "file_refs": _file_refs_from_value(report),
                "artifacts": payload.get("artifact_refs", []),
            }
        )
    if event.event_kind == "team_idle_waiting":
        result.append(
            {
                "teammate_id": event.agent_id,
                "report_type": "idle",
                "progress_state": "working",
                "summary": str(payload.get("wait_reason") or "team idle waiting"),
                "completed_items": [],
                "open_items": [],
                "blocked_items": [],
                "evidence_refs": [],
                "file_refs": _file_refs_from_value(payload),
                "owned_process_refs": payload.get("owned_process_refs", []),
                "artifacts": payload.get("partial_artifact_refs", []),
            }
        )
    return result


def _artifacts_from_event(event: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
    artifact_refs = payload.get("artifact_refs")
    if not isinstance(artifact_refs, list):
        bridge_result = payload.get("bridge_result") if isinstance(payload.get("bridge_result"), dict) else {}
        artifact_refs = bridge_result.get("artifact_refs") if isinstance(bridge_result.get("artifact_refs"), list) else []
    return [
        {
            "artifact_ref": str(ref),
            "artifact_type": "path_or_ref",
            "summary": f"artifact recorded from {event.event_kind}",
            "status": "recorded",
        }
        for ref in artifact_refs
    ]


def _completion_check_from_event(event: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
    if event.event_kind not in {"completion_contract_satisfied", "completion_contract_rejected"}:
        return None
    checks = payload.get("completion_checks") if isinstance(payload.get("completion_checks"), dict) else {}
    missing = []
    for key in ("missing_outputs", "missing_artifacts", "failed_validations", "missing_contract_items"):
        values = checks.get(key) or payload.get(key) or []
        if isinstance(values, list):
            missing.extend(values)
    return {
        "check_type": "completion_contract",
        "status": "satisfied" if event.event_kind == "completion_contract_satisfied" else "rejected",
        "missing": missing,
        "items": _completion_items(payload, checks, missing),
        "summary": _summarize(checks or payload),
        "completion_checks": checks,
        "completion_contract": payload.get("completion_contract", {}),
    }


def _process_events_from_event(event: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if event.event_kind != "team_idle_waiting":
        return []
    refs = payload.get("owned_process_refs")
    if not isinstance(refs, list):
        return []
    result = []
    for index, ref in enumerate(refs, start=1):
        data = ref if isinstance(ref, dict) else {"process_ref": str(ref)}
        result.append(
            {
                "process_ref": data.get("process_ref") or data.get("id") or data.get("pid") or f"process_{index}",
                "pid": data.get("pid"),
                "command_preview": _redact_text(str(data.get("command") or data.get("command_preview") or ""))[:500],
                "started_at": data.get("started_at"),
                "last_heartbeat_at": payload.get("last_heartbeat_at"),
                "state": _process_state(data),
                "exit_code": data.get("exit_code"),
                "log_tail_ref": data.get("log_tail_ref") or data.get("log_path"),
                "artifact_probe": payload.get("artifact_probe", {}),
                "summary": f"process {data.get('pid') or index} {_process_state(data)}",
            }
        )
    return result


def _report_type_for_event(event_kind: str) -> str:
    if "partial" in event_kind or event_kind == "team_idle_waiting":
        return "partial"
    if "failure" in event_kind or "failed" in event_kind:
        return "failed"
    if "completion" in event_kind or "returned" in event_kind:
        return "final"
    return "progress"


def _progress_state_for_event(event_kind: str, report: Any) -> str:
    if isinstance(report, dict):
        explicit = report.get("progress_state") or report.get("status")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
    if "failed" in event_kind or "failure" in event_kind:
        return "failed"
    if "partial" in event_kind:
        return "partial"
    if "completion" in event_kind or "returned" in event_kind:
        return "done"
    return "working"


def _action_for_tool(tool_name: str) -> str:
    mapping = {
        "Read": "read_file",
        "Grep": "search_text",
        "Glob": "match_files",
        "LS": "list_directory",
        "Edit": "edit_file",
        "Write": "write_file",
        "MultiEdit": "edit_file",
        "Bash": "run_command",
    }
    return mapping.get(tool_name, tool_name)


def _target_from_payload(payload: dict[str, Any]) -> str | None:
    for source in (payload, payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}):
        for key in ("file_path", "path", "target", "command", "pattern"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _normalized_input(payload: dict[str, Any]) -> dict[str, Any]:
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else payload
    return {key: value for key, value in _safe_input_preview({"tool_input": tool_input}).items() if value is not None}


def _safe_input_preview(payload: dict[str, Any]) -> dict[str, Any]:
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else payload
    preview = {}
    for key in ("file_path", "path", "command", "pattern", "glob", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            preview[key] = _redact_text(value)[:500]
    return preview


def _file_refs(tool_name: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else payload
    role = {
        "Read": "read",
        "Edit": "edit",
        "Write": "write",
        "MultiEdit": "edit",
        "Grep": "search",
        "Glob": "search",
        "LS": "cwd",
        "Bash": "cwd",
    }.get(tool_name, "artifact")
    refs = []
    for key in ("file_path", "path", "target"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            refs.append(_file_ref(value, role))
    if tool_name == "Bash":
        cwd = tool_input.get("cwd") or tool_input.get("workdir")
        if isinstance(cwd, str) and cwd.strip():
            refs.append(_file_ref(cwd, "cwd"))
    return _dedupe_file_refs(refs)


def _file_refs_from_value(value: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(nested, str) and ("path" in str(key).lower() or "file" in str(key).lower() or "artifact" in str(key).lower()):
                refs.append(_file_ref(nested, "artifact"))
            elif isinstance(nested, (dict, list)):
                refs.extend(_file_refs_from_value(nested))
    elif isinstance(value, list):
        for item in value[:20]:
            refs.extend(_file_refs_from_value(item))
    return _dedupe_file_refs(refs)[:20]


def _file_ref(path_text: str, role: str) -> dict[str, Any]:
    path = str(path_text).strip()
    exists = Path(path).exists() if path else False
    return {
        "path": path,
        "role": role,
        "exists_before": exists,
        "exists_after": exists,
    }


def _dedupe_file_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for ref in refs:
        key = (ref.get("path"), ref.get("role"))
        if ref.get("path") and key not in seen:
            result.append(ref)
            seen.add(key)
    return result


def _output_summary(payload: dict[str, Any], *, status: str) -> dict[str, Any]:
    stdout = str(payload.get("stdout") or payload.get("stdout_tail") or "")
    stderr = str(payload.get("stderr") or payload.get("stderr_tail") or "")
    exit_code = payload.get("exit_code")
    return {
        "stdout_lines": len(stdout.splitlines()) if stdout else 0,
        "stderr_lines": len(stderr.splitlines()) if stderr else 0,
        "truncated": len(stdout) > 1200 or len(stderr) > 1200,
        "notable": f"status {status}" if exit_code is None else f"command exited {exit_code}",
    }


def _completion_items(payload: dict[str, Any], checks: dict[str, Any], missing: list[Any]) -> list[dict[str, Any]]:
    contract = payload.get("completion_contract") if isinstance(payload.get("completion_contract"), dict) else {}
    items = []
    for key, label in (
        ("required_outputs", "output"),
        ("required_artifacts", "artifact"),
        ("validation_requirements", "validation"),
    ):
        for index, text in enumerate(contract.get(key, []) if isinstance(contract.get(key), list) else [], start=1):
            status = "missing" if text in missing else "satisfied"
            if key == "validation_requirements" and text in checks.get("failed_validations", []):
                status = "failed"
            items.append(
                {
                    "id": f"{label}_{index}",
                    "text": str(text),
                    "status": status,
                    "evidence_refs": [],
                    "reason": "missing from completion checks" if status in {"missing", "failed"} else "",
                }
            )
    if not items and missing:
        for index, text in enumerate(missing, start=1):
            items.append({"id": f"missing_{index}", "text": str(text), "status": "missing", "evidence_refs": [], "reason": "reported missing"})
    return items


def _coverage_refs(packet: dict[str, Any]) -> list[str]:
    task = packet.get("task_spec") if isinstance(packet.get("task_spec"), dict) else {}
    refs = task.get("instruction_coverage_checklist")
    return [str(item) for item in refs] if isinstance(refs, list) else []


def _list_field(value: Any, key: str) -> list[Any]:
    if isinstance(value, dict) and isinstance(value.get(key), list):
        return value[key]
    return []


def _process_state(ref: dict[str, Any]) -> str:
    state = str(ref.get("state") or ref.get("status") or ref.get("process_status") or "").strip().lower()
    if state in {"running", "exited", "failed", "unknown"}:
        return state
    if state in {"success", "succeeded", "completed", "complete"}:
        return "exited"
    if state in {"error", "dead", "terminated"}:
        return "failed"
    return "unknown" if not state else state


def _redact_text(text: str) -> str:
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)(\s*[:=]\s*)(\S+)", r"\1\2<redacted>", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-<redacted>", text)
    return text


def _next_sequence(path: Path) -> int:
    if not path.exists():
        return 1
    try:
        with path.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip()) + 1
    except Exception:
        return 1


def _first_line_role(text: str) -> str | None:
    first = str(text or "").splitlines()[0].strip() if text else ""
    if ":" in first:
        return first.split(":", 1)[0].strip()
    return None


def _summarize(value: Any, *, limit: int = 240) -> str:
    if isinstance(value, str):
        text = value
    else:
        import json

        text = json.dumps(sanitize_json_value(value), ensure_ascii=False, default=str)
    return " ".join(text.split())[:limit]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
