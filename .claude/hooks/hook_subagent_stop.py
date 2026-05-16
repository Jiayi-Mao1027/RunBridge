from __future__ import annotations

import json
from typing import Any

from common import emit_observer_record, now_iso, observer_binding, read_hook_input, redact_observer_text


REPORT_KEYS = {"summary", "instruction_coverage", "evidence", "evidence_refs", "completed_items", "blocked_items", "open_items"}
TEXT_KEYS = (
    "result",
    "response",
    "final_response",
    "final_text",
    "assistant_text",
    "message",
    "content",
    "text",
    "output",
)
MAX_REPORT_TEXT_CHARS = 1200
MAX_REPORT_LIST_ITEMS = 40
MAX_REPORT_DICT_ITEMS = 80
MAX_REPORT_DEPTH = 5
MAX_JSON_CANDIDATE_CHARS = 200_000
TRUNCATED_MARKER = "...[truncated]"
SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)


def main() -> int:
    payload = read_hook_input()
    timestamp = now_iso()
    binding = observer_binding(payload)
    final_text = _find_text(payload)
    report = _find_report(payload, final_text)
    preview = _bounded_text(final_text or "subagent stopped")

    emit_observer_record(
        "session_events",
        {
            "timestamp": timestamp,
            **binding,
            "event_type": "subagent_stopped",
            "message_preview": preview,
            "payload_keys": sorted(str(key) for key in payload.keys())[:40],
            "cwd": payload.get("cwd"),
            "project_root": payload.get("project_root"),
        },
    )

    if report and binding.get("run_id") and binding.get("teammate_id"):
        emit_observer_record(
            "teammate_reports",
            {
                "timestamp": timestamp,
                **binding,
                "report_type": "subagent_final",
                "progress_state": "completed",
                "summary": _summary(report, preview),
                "report": report,
                "completed_items": _list_field(report, "completed_items"),
                "open_items": _list_field(report, "open_items"),
                "blocked_items": _list_field(report, "blocked_items"),
                "evidence_refs": _list_field(report, "evidence_refs"),
                "file_refs": _list_field(report, "file_refs"),
                "artifacts": _list_field(report, "artifact_refs"),
            },
        )
    return 0


def _find_text(value: Any, *, _depth: int = 0) -> str | None:
    if _depth > 4:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in TEXT_KEYS:
            text = _find_text(value.get(key), _depth=_depth + 1)
            if text:
                return text
        for nested_key in ("tool_response", "payload", "event", "subagent", "result_message"):
            text = _find_text(value.get(nested_key), _depth=_depth + 1)
            if text:
                return text
    if isinstance(value, list):
        parts = []
        for item in value[:20]:
            text = _find_text(item, _depth=_depth + 1)
            if text:
                parts.append(text)
        if parts:
            return "\n".join(parts)
    return None


def _find_report(payload: dict[str, Any], final_text: str | None) -> dict[str, Any] | None:
    for key in ("report", "structured_output", "result", "tool_response"):
        report = _coerce_report(payload.get(key))
        if report:
            return report
    if final_text:
        report = _coerce_report(final_text)
        if report:
            return report
    return None


def _coerce_report(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if REPORT_KEYS.intersection(value.keys()):
            return _sanitize_report(value)
        for key in ("report", "structured_output", "result"):
            report = _coerce_report(value.get(key))
            if report:
                return report
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        for candidate in _json_candidates(text):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            report = _coerce_report(parsed)
            if report:
                return report
    return None


def _json_candidates(text: str) -> list[str]:
    candidates = [text] if len(text) <= MAX_JSON_CANDIDATE_CHARS else []
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidate = text[start : end + 1]
        if len(candidate) <= MAX_JSON_CANDIDATE_CHARS:
            candidates.append(candidate)
    return candidates


def _sanitize_report(report: dict[str, Any]) -> dict[str, Any]:
    sanitized = _sanitize_report_value(report, _depth=0)
    return sanitized if isinstance(sanitized, dict) else {}


def _sanitize_report_value(value: Any, *, _key: str | None = None, _depth: int = 0) -> Any:
    if _is_sensitive_key(_key):
        return "<redacted>"
    if _depth >= MAX_REPORT_DEPTH:
        if isinstance(value, str):
            return _bounded_text(value)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return "<truncated>"
    if isinstance(value, str):
        return _bounded_text(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_REPORT_DICT_ITEMS:
                result["_truncated_keys"] = len(value) - MAX_REPORT_DICT_ITEMS
                break
            key_text = str(key)
            result[key_text] = _sanitize_report_value(item, _key=key_text, _depth=_depth + 1)
        return result
    if isinstance(value, list):
        return [_sanitize_report_value(item, _depth=_depth + 1) for item in value[:MAX_REPORT_LIST_ITEMS]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _bounded_text(str(value))


def _is_sensitive_key(key: str | None) -> bool:
    if not key:
        return False
    lowered = key.replace("-", "_").lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _bounded_text(text: str, limit: int = MAX_REPORT_TEXT_CHARS) -> str:
    redacted = redact_observer_text(text)
    if len(redacted) <= limit:
        return redacted
    marker = TRUNCATED_MARKER
    if limit <= len(marker):
        return redacted[:limit]
    return redacted[: limit - len(marker)] + marker


def _summary(report: dict[str, Any], fallback: str) -> str:
    summary = report.get("summary")
    if isinstance(summary, str) and summary.strip():
        return _bounded_text(summary.strip())
    return fallback


def _list_field(report: dict[str, Any], key: str) -> list[Any]:
    value = report.get(key)
    if not isinstance(value, list):
        return []
    return [_sanitize_report_value(item, _depth=1) for item in value[:MAX_REPORT_LIST_ITEMS]]


if __name__ == "__main__":
    raise SystemExit(main())
