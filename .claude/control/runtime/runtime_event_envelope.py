from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any


ENVELOPE_SCHEMA_VERSION = "runtime_event_envelope.v1"
VALID_SOURCES = {"outer_sdk", "inner_sdk", "cli", "hook", "runtime", "companion"}
VALID_AUTHORITIES = {"authoritative", "source", "observed", "derived", "projection"}
PREVIEW_LIMIT = 700


def normalize_runtime_event(
    event: Any,
    *,
    source: str = "runtime",
    authority: str = "authoritative",
    seq: int | None = None,
    phase: str | None = None,
    caused_by: str | None = None,
    payload_ref: str | None = None,
    safe_preview: Any = None,
) -> dict[str, Any]:
    payload = _payload_from_event(event)
    event_kind = _get(event, "event_kind") or payload.get("event_kind") or payload.get("event_type")
    timestamp = _get(event, "timestamp") or payload.get("timestamp") or _now_iso()
    run_id = _get(event, "run_id") or payload.get("run_id")
    main_session_id = _get(event, "main_session_id") or payload.get("main_session_id")
    sub_session_id = _get(event, "sub_session_id") or payload.get("sub_session_id")
    session_id = payload.get("session_id") or sub_session_id or main_session_id
    event_id = _get(event, "event_id") or payload.get("event_id") or _stable_event_id(payload, event_kind, timestamp)
    envelope = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "event_id": str(event_id) if event_id else None,
        "run_id": str(run_id) if run_id else None,
        "session_id": str(session_id) if session_id else None,
        "window_id": _optional_str(_get(event, "bridge_window_id") or payload.get("bridge_window_id") or payload.get("window_id")),
        "team_id": _optional_str(_get(event, "team_id") or payload.get("team_id")),
        "task_id": _optional_str(_get(event, "task_id") or payload.get("task_id")),
        "agent_id": _optional_str(_get(event, "agent_id") or payload.get("agent_id")),
        "phase": phase or _optional_str(payload.get("phase") or payload.get("target_phase")),
        "event_kind": str(event_kind or "runtime_event"),
        "source": _source(source),
        "seq": int(seq) if seq is not None else _positive_int(payload.get("sequence") or payload.get("monotonic_index")),
        "timestamp": str(timestamp),
        "caused_by": caused_by or _optional_str(_get(event, "parent_event_id") or payload.get("parent_event_id") or payload.get("source_event_id")),
        "payload_ref": payload_ref or _optional_str(_get(event, "payload_ref") or payload.get("payload_ref")),
        "safe_preview": _safe_preview(safe_preview if safe_preview is not None else payload),
        "authority": _authority(authority),
    }
    return envelope


def normalize_stream_record(
    record: dict[str, Any],
    *,
    source: str,
    authority: str = "observed",
    event_kind: str | None = None,
    seq: int | None = None,
    payload_ref: str | None = None,
) -> dict[str, Any]:
    kind = event_kind or str(record.get("event_kind") or record.get("event_type") or record.get("type") or "stream_event")
    envelope_input = {
        **record,
        "event_kind": kind,
        "event_id": record.get("event_id") or record.get("source_event_id"),
    }
    return normalize_runtime_event(
        envelope_input,
        source=source,
        authority=authority,
        seq=seq if seq is not None else _positive_int(record.get("sequence") or record.get("monotonic_index")),
        payload_ref=payload_ref,
        safe_preview=_preview_source(record),
    )


def attach_runtime_event_envelope(
    record: dict[str, Any],
    *,
    source: str,
    authority: str,
    event_kind: str | None = None,
    seq: int | None = None,
    payload_ref: str | None = None,
) -> dict[str, Any]:
    return {
        **record,
        "runtime_event": normalize_stream_record(
            record,
            source=source,
            authority=authority,
            event_kind=event_kind,
            seq=seq,
            payload_ref=payload_ref,
        ),
    }


def _payload_from_event(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        payload = event.get("payload")
        if isinstance(payload, dict):
            return {**event, **payload}
        return event
    payload = getattr(event, "payload", None)
    base: dict[str, Any] = {}
    for key in (
        "event_id",
        "run_id",
        "main_session_id",
        "sub_session_id",
        "bridge_window_id",
        "team_id",
        "task_id",
        "agent_id",
        "agent_type",
        "event_kind",
        "timestamp",
        "payload_ref",
        "parent_event_id",
    ):
        value = getattr(event, key, None)
        if value is not None:
            base[key] = value
    if isinstance(payload, dict):
        base.update(payload)
    return base


def _get(event: Any, key: str) -> Any:
    if isinstance(event, dict):
        return event.get(key)
    return getattr(event, key, None)


def _source(value: str) -> str:
    text = str(value or "").strip()
    return text if text in VALID_SOURCES else "runtime"


def _authority(value: str) -> str:
    text = str(value or "").strip()
    return text if text in VALID_AUTHORITIES else "observed"


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _safe_preview(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        except Exception:
            text = str(value)
    return _redact(text)[:PREVIEW_LIMIT]


def _preview_source(record: dict[str, Any]) -> Any:
    for key in ("safe_preview", "message_preview", "summary", "text_delta", "target", "event_kind", "event_type"):
        value = record.get(key)
        if value:
            return value
    return {key: record.get(key) for key in ("status", "tool_name", "source_event_kind") if key in record}


def _stable_event_id(payload: dict[str, Any], event_kind: Any, timestamp: Any) -> str:
    raw = json.dumps(
        {
            "run_id": payload.get("run_id"),
            "session_id": payload.get("session_id") or payload.get("sub_session_id") or payload.get("main_session_id"),
            "event_kind": event_kind,
            "timestamp": timestamp,
            "sequence": payload.get("sequence") or payload.get("monotonic_index"),
        },
        sort_keys=True,
        default=str,
    )
    return "evt_norm_" + hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _redact(text: str) -> str:
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)(\s*[:=]\s*)(\S+)", r"\1\2<redacted>", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-<redacted>", text)
    return text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

