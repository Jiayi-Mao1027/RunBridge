from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


ARTIFACT_REF_SCHEMA_VERSION = "artifact_ref.v1"
PREVIEW_LIMIT = 500


def normalize_artifact_refs(
    refs: Any,
    *,
    context: dict[str, Any] | None = None,
    base_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(refs, list):
        return []
    return [normalize_artifact_ref(ref, context=context, base_dir=base_dir) for ref in refs]


def normalize_artifact_ref(
    ref: Any,
    *,
    context: dict[str, Any] | None = None,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    context = context or {}
    if isinstance(ref, dict):
        data = dict(ref)
        ref_type = str(data.get("ref_type") or _infer_ref_type(data.get("path") or data.get("id") or data.get("safe_preview")) or "artifact")
        path_text = _optional_str(data.get("path"))
        ref_id = _optional_str(data.get("id")) or _stable_ref_id(ref_type, path_text or data.get("safe_preview") or data)
        sha = _optional_str(data.get("sha256"))
        preview = _optional_str(data.get("safe_preview")) or _safe_preview(path_text or ref_id)
    else:
        text = str(ref)
        ref_type = _infer_ref_type(text)
        path_text = text if ref_type in {"path", "log_manifest", "file"} else None
        ref_id = _stable_ref_id(ref_type, text)
        sha = None
        preview = _safe_preview(text)
    created_at = _optional_str(data.get("created_at")) if isinstance(ref, dict) else None

    resolved_path = _resolve_path(path_text, base_dir)
    if resolved_path and resolved_path.is_file():
        sha = sha or _sha256_file(resolved_path)

    source = data if isinstance(ref, dict) else {}
    producer = {
        "agent_id": context.get("agent_id"),
        "event_id": context.get("event_id"),
    }
    existing_producer = source.get("producer") if isinstance(source.get("producer"), dict) else {}
    producer.update({key: value for key, value in existing_producer.items() if value})

    return {
        "schema_version": ARTIFACT_REF_SCHEMA_VERSION,
        "ref_type": ref_type,
        "id": ref_id,
        "path": str(resolved_path) if resolved_path else path_text,
        "sha256": sha,
        "producer": producer,
        "created_at": created_at or context.get("timestamp") or _now_iso(),
        "safe_preview": preview,
        "run_id": _optional_str(source.get("run_id")) or context.get("run_id"),
        "bridge_window_id": _optional_str(source.get("bridge_window_id") or source.get("window_id")) or context.get("bridge_window_id") or context.get("window_id"),
        "team_id": _optional_str(source.get("team_id")) or context.get("team_id"),
        "task_id": _optional_str(source.get("task_id")) or context.get("task_id"),
    }


def validate_artifact_refs(
    refs: Any,
    *,
    required_artifacts: Any = None,
    context: dict[str, Any] | None = None,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    context = context or {}
    normalized = normalize_artifact_refs(refs, context=context, base_dir=base_dir)
    checks: list[dict[str, Any]] = []
    missing: list[str] = []

    required = [str(item) for item in required_artifacts] if isinstance(required_artifacts, list) else []
    required_ref_keys: set[tuple[str, str, str, str]] = set()
    for requirement in required:
        matches = [ref for ref in normalized if artifact_ref_satisfies(requirement, ref)]
        usable_matches = [ref for ref in matches if _required_ref_is_usable(requirement, ref, context=context)]
        if usable_matches:
            required_ref_keys.add(_artifact_ref_instance_key(usable_matches[0]))
            checks.append(_check("artifact_required", "pass", requirement, evidence_ref=usable_matches[0].get("id")))
        elif matches:
            missing.append(requirement)
            checks.append(_check("artifact_required", "block", requirement, message="required artifact path is missing or unreadable"))
        else:
            missing.append(requirement)
            checks.append(_check("artifact_required", "block", requirement, message="required artifact missing"))

    for ref in normalized:
        checks.extend(_validate_one_ref(ref, context=context, required_ref_keys=required_ref_keys))

    return {
        "valid": not any(item["status"] in {"fail", "block"} for item in checks),
        "normalized_refs": normalized,
        "missing_required_artifacts": missing,
        "checks": checks,
    }


def artifact_ref_satisfies(required: str, ref: dict[str, Any]) -> bool:
    key = required.strip().casefold()
    if not key:
        return True
    searchable = {
        "ref_type": ref.get("ref_type"),
        "id": ref.get("id"),
        "path": ref.get("path"),
        "safe_preview": ref.get("safe_preview"),
    }
    haystack = json.dumps(searchable, ensure_ascii=False, default=str).casefold().replace("\\", "/")
    if key in {"artifact", "artifacts"}:
        ref_type = str(ref.get("ref_type") or "").casefold()
        return ref_type in {"artifact", "logical"} and "manifest" not in haystack
    if key == "log_manifest":
        return artifact_ref_is_log_manifest(ref)
    return key in haystack


def _validate_one_ref(ref: dict[str, Any], *, context: dict[str, Any], required_ref_keys: set[tuple[str, str, str, str]] | None = None) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    required_ref_keys = required_ref_keys or set()
    is_required_ref = _artifact_ref_instance_key(ref) in required_ref_keys
    if ref.get("schema_version") == ARTIFACT_REF_SCHEMA_VERSION:
        checks.append(_check("artifact_schema", "pass", str(ref.get("id"))))
    else:
        checks.append(_check("artifact_schema", "fail", str(ref.get("id")), message="artifact ref schema_version mismatch"))

    for key in ("run_id", "bridge_window_id", "team_id", "task_id"):
        expected = context.get(key) or (context.get("window_id") if key == "bridge_window_id" else None)
        actual = ref.get(key)
        if expected and actual and str(expected) != str(actual):
            checks.append(
                _check(
                    "artifact_binding",
                    "block" if is_required_ref else "warn",
                    str(ref.get("id")),
                    message=f"{key} does not match current context",
                )
            )

    path_text = _optional_str(ref.get("path"))
    if path_text:
        path = Path(path_text)
        if path.exists() and path.is_file():
            actual_sha = _sha256_file(path)
            if ref.get("sha256") and ref.get("sha256") != actual_sha:
                checks.append(_check("artifact_hash", "block", str(ref.get("id")), message="sha256 mismatch"))
            else:
                checks.append(_check("artifact_hash", "pass", str(ref.get("id")), evidence_ref=path_text))
        elif ref.get("ref_type") in {"path", "file", "log_manifest"}:
            checks.append(_check("artifact_exists", "warn", str(ref.get("id")), message="path artifact not found in current filesystem"))
    else:
        checks.append(_check("artifact_exists", "warn", str(ref.get("id")), message="logical artifact ref has no path to verify"))
    return checks


def _required_ref_is_usable(requirement: str, ref: dict[str, Any], *, context: dict[str, Any] | None = None) -> bool:
    ref_type = str(ref.get("ref_type") or "").strip().casefold()
    requirement_key = str(requirement or "").strip().casefold()
    file_backed = ref_type in {"path", "file", "log_manifest"} or requirement_key == "log_manifest"
    if not file_backed:
        return True
    path_text = _optional_str(ref.get("path"))
    if not bool(path_text and Path(path_text).is_file()):
        return False
    if requirement_key == "log_manifest" and ref_type == "log_manifest" and context:
        for key in ("run_id", "bridge_window_id", "task_id"):
            expected = context.get(key) or (context.get("window_id") if key == "bridge_window_id" else None)
            actual = ref.get(key)
            if expected and actual and str(expected) != str(actual):
                return False
    return True


def _artifact_ref_instance_key(ref: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(ref.get("id") or ""),
        str(ref.get("path") or ""),
        str(ref.get("bridge_window_id") or ""),
        str(ref.get("task_id") or ""),
    )


def _check(name: str, status: str, subject: str, *, message: str = "", evidence_ref: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "subject": subject,
        "message": message,
        "evidence_ref": evidence_ref,
    }


def _infer_ref_type(value: Any) -> str:
    text = str(value or "").casefold().replace("\\", "/")
    if _text_looks_like_formal_log_manifest(text):
        return "log_manifest"
    if "/" in text or re.search(r"\.[a-z0-9]{1,8}$", text):
        return "path"
    return "logical"


def artifact_ref_is_log_manifest(ref: dict[str, Any]) -> bool:
    candidates = [
        ref.get("path"),
        ref.get("safe_preview"),
        ref.get("id"),
    ]
    if any(_text_looks_like_formal_log_manifest(str(candidate or "")) for candidate in candidates):
        return True
    ref_type = str(ref.get("ref_type") or "").strip().casefold()
    has_pathish_candidate = any(str(candidate or "").strip() for candidate in candidates)
    return ref_type == "log_manifest" and not has_pathish_candidate


def _text_looks_like_formal_log_manifest(value: str) -> bool:
    text = str(value or "").strip().casefold().replace("\\", "/")
    if text == "log_manifest":
        return True
    if not text:
        return False
    basename = PurePosixPath(text).name
    if basename in {"log_manifest.json", "execute_log_manifest.json"}:
        return True
    if basename.endswith("_log_manifest.json") or basename.endswith("-log-manifest.json"):
        return True
    if re.search(r"(?:^|[_-])log[_-]manifest(?:[_-].*)?\.json$", basename):
        return True
    if basename == "manifest.json":
        parts = [part for part in text.split("/") if part]
        return any(part in {"log", "logs", "execute", "execution"} or part.endswith("_logs") for part in parts[:-1])
    return False


def _resolve_path(path_text: str | None, base_dir: str | Path | None) -> Path | None:
    if not path_text:
        return None
    path = _path_candidate(path_text, base_dir)
    if path.exists():
        return _safe_resolve(path)
    repaired_text = _repair_tui_wrapped_path(path_text)
    if repaired_text != path_text:
        repaired_path = _path_candidate(repaired_text, base_dir)
        if repaired_path.exists():
            return _safe_resolve(repaired_path)
    return _safe_resolve(path)


def _path_candidate(path_text: str, base_dir: str | Path | None) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = Path(base_dir).expanduser().resolve() / path
    return path


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except Exception:
        return path


def _repair_tui_wrapped_path(path_text: str) -> str:
    return re.sub(r"\s{2,}", "", path_text)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_ref_id(ref_type: str, value: Any) -> str:
    raw = json.dumps({"ref_type": ref_type, "value": value}, ensure_ascii=False, sort_keys=True, default=str)
    return "art_" + hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _safe_preview(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)(\s*[:=]\s*)(\S+)", r"\1\2<redacted>", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-<redacted>", text)
    return text[:PREVIEW_LIMIT]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
