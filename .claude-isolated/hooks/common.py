#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONFIG_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = CONFIG_ROOT / "runtime_state"
EVENT_LOG = RUNTIME_ROOT / "event_log.jsonl"
OWNED_PATH = RUNTIME_ROOT / "process_guard" / "owned.json"
GPU_PROBED_FLAG = RUNTIME_ROOT / "gpu_probed"
CHECKPOINT_PATH = RUNTIME_ROOT / "checkpoint.json"
SESSION_START_PATH = RUNTIME_ROOT / "session_start_last.json"
TOUCHED_FILES_LOG = RUNTIME_ROOT / "touched_files.jsonl"
PROJECTS_ROOT = RUNTIME_ROOT / "projects"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(path: Path, data: dict[str, Any]) -> None:
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False) + "\n")


def load_hook_input() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {"raw_payload": payload}
    except Exception:
        return {"raw_stdin": raw}


def project_root(payload: dict[str, Any]) -> Path:
    candidates = [
        os.environ.get("CLAUDE_PROJECT_DIR"),
        payload.get("cwd"),
        Path.cwd(),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser().resolve()
        if path.exists():
            return path
    return Path.cwd().resolve()


def slugify(value: str, default: str = "unknown") -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-").lower()
    return slug or default


def project_key(root: Path) -> str:
    digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:12]
    return f"{slugify(root.name)}-{digest}"


def project_state_root(payload: dict[str, Any]) -> Path:
    return PROJECTS_ROOT / project_key(project_root(payload))


def protocol_dir(payload: dict[str, Any], name: str) -> Path:
    path = project_state_root(payload) / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def append_event(payload: dict[str, Any], event_type: str, **fields: Any) -> None:
    event = {
        "type": event_type,
        "timestamp": now_iso(),
        "project_root": str(project_root(payload)),
    }
    event.update(fields)
    append_jsonl(EVENT_LOG, event)


def recent_events(limit: int = 20) -> list[dict[str, Any]]:
    if not EVENT_LOG.exists():
        return []
    try:
        lines = EVENT_LOG.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    items: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                items.append(parsed)
        except Exception:
            continue
    return items


def list_json_files(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    return sorted([item for item in path.iterdir() if item.suffix == ".json"])


def load_json_files(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for json_file in list_json_files(path):
        payload = read_json(json_file, {})
        if isinstance(payload, dict):
            payload.setdefault("_path", str(json_file))
            items.append(payload)
    return items


def infer_layer(team_name: str = "", teammate_name: str = "", task_kind: str = "") -> str:
    haystack = " ".join([team_name, teammate_name, task_kind]).lower()
    if any(token in haystack for token in ("chiefmate", "advisory", "brain")):
        return "L2"
    if any(token in haystack for token in ("refresher", "curator", "preflight", "bridge")):
        return "L3"
    if any(
        token in haystack
        for token in ("implement", "rungater", "executor", "postrun", "anomaly", "practice")
    ):
        return "L4"
    return "unknown"


def parse_tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    tool_input = payload.get("tool_input", {})
    return tool_input if isinstance(tool_input, dict) else {}


def parse_command(payload: dict[str, Any]) -> str:
    tool_input = parse_tool_input(payload)
    if isinstance(tool_input.get("command"), str):
        return tool_input["command"]
    if isinstance(payload.get("command"), str):
        return payload["command"]
    return ""


def parse_tool_response(payload: dict[str, Any]) -> tuple[str, int]:
    tool_response = payload.get("tool_response")
    if isinstance(tool_response, dict):
        stdout = tool_response.get("stdout") or tool_response.get("output") or ""
        exit_code = tool_response.get("exit_code", tool_response.get("exitCode", 0))
        try:
            return str(stdout), int(exit_code)
        except Exception:
            return str(stdout), 0
    if isinstance(tool_response, str):
        raw = tool_response.strip()
        if not raw:
            return "", 0
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                stdout = decoded.get("stdout") or decoded.get("output") or ""
                exit_code = decoded.get("exit_code", decoded.get("exitCode", 0))
                return str(stdout), int(exit_code)
        except Exception:
            return raw, 0
    return "", 0


def deny_pre_tool(reason: str) -> int:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            ensure_ascii=False,
        )
    )
    return 0


def system_message(message: str) -> int:
    print(json.dumps({"systemMessage": message}, ensure_ascii=False))
    return 0


def block(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def tool_paths(payload: dict[str, Any]) -> list[Path]:
    tool_input = parse_tool_input(payload)
    root = project_root(payload)
    results: list[Path] = []
    candidate_keys = ("file_path", "path", "paths", "file_paths")
    for key in candidate_keys:
        value = tool_input.get(key)
        if isinstance(value, str):
            results.append(resolve_path(value, root))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    results.append(resolve_path(item, root))
    return dedupe_paths(results)


def resolve_path(value: str, root: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


def dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def relative_to_project(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except Exception:
        return str(path)


def parse_embedded_json(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text:
        return {}
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    if not text.startswith("{"):
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def envelope_path(payload: dict[str, Any], task_id: str) -> Path:
    return protocol_dir(payload, "task_envelopes") / f"{slugify(task_id)}.json"


def status_path(payload: dict[str, Any], task_id: str, teammate_name: str) -> Path:
    name = f"{slugify(task_id)}__{slugify(teammate_name or 'unknown')}.json"
    return protocol_dir(payload, "teammate_status") / name


def receipt_path(payload: dict[str, Any], task_id: str) -> Path:
    return protocol_dir(payload, "completion_receipts") / f"{slugify(task_id)}.json"


def load_envelope(payload: dict[str, Any], task_id: str) -> dict[str, Any]:
    return read_json(envelope_path(payload, task_id), {}) or {}


def load_receipt(payload: dict[str, Any], task_id: str) -> dict[str, Any]:
    return read_json(receipt_path(payload, task_id), {}) or {}


def active_task_ids(payload: dict[str, Any]) -> list[str]:
    envelope_dir = protocol_dir(payload, "task_envelopes")
    receipt_dir = protocol_dir(payload, "completion_receipts")
    receipts = {item.stem for item in list_json_files(receipt_dir)}
    active: list[str] = []
    for envelope_file in list_json_files(envelope_dir):
        if envelope_file.stem not in receipts:
            payload_item = read_json(envelope_file, {})
            task_id = payload_item.get("task_id") if isinstance(payload_item, dict) else None
            active.append(str(task_id or envelope_file.stem))
    return active


def latest_completed_task_ids(payload: dict[str, Any], limit: int = 5) -> list[str]:
    receipt_dir = protocol_dir(payload, "completion_receipts")
    receipts = sorted(
        list_json_files(receipt_dir),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    task_ids: list[str] = []
    for receipt_file in receipts[:limit]:
        receipt = read_json(receipt_file, {})
        task_ids.append(str(receipt.get("task_id") or receipt_file.stem))
    return task_ids


def owned_process_snapshot() -> list[dict[str, Any]]:
    payload = read_json(OWNED_PATH, {"items": []}) or {"items": []}
    items = payload.get("items", [])
    return [item for item in items if isinstance(item, dict)]


def pid_ppid_map() -> dict[int, int]:
    out = subprocess.check_output(["ps", "-e", "-o", "pid=,ppid="], text=True)
    mapping: dict[int, int] = {}
    for raw in out.splitlines():
        parts = raw.strip().split()
        if len(parts) != 2:
            continue
        pid_s, ppid_s = parts
        if pid_s.isdigit() and ppid_s.isdigit():
            mapping[int(pid_s)] = int(ppid_s)
    return mapping


def proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except Exception:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()

