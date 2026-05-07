from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_hook_input() -> dict[str, Any]:
    raw_bytes = sys.stdin.buffer.read()
    if not raw_bytes.strip():
        return {}
    try:
        raw = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raw = raw_bytes.decode(sys.stdin.encoding or "utf-8", errors="replace")
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {"raw_payload": payload}
    except Exception:
        return {"raw_stdin": raw}


def script_root() -> Path:
    return Path(__file__).resolve().parent


def claude_root() -> Path:
    return script_root().parent


def control_root() -> Path:
    return claude_root() / "control"


def runtime_root() -> Path:
    return control_root() / "runtime"


def runtime_main() -> Path:
    return runtime_root() / "main.py"


def runtime_runs_root() -> Path:
    configured = os.environ.get("BRIDGE_RUNTIME_RUNS_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    project_root = Path(os.environ.get("BRIDGE_PROJECT_ROOT") or os.getcwd()).resolve()
    return claude_root() / "runtime_state" / "projects" / project_state_key(project_root) / "runs"


def active_run_path() -> Path:
    return runtime_runs_root() / ".active_run.json"


def read_active_run(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    path = active_run_path()
    if not path.exists():
        return {}
    try:
        active = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(active, dict):
        return {}
    payload = payload or {}
    session_id = str(payload.get("session_id") or payload.get("sessionId") or payload.get("main_session_id") or "").strip()
    active_session_id = str(active.get("session_id") or active.get("main_session_id") or "").strip()
    if session_id and active_session_id and session_id != active_session_id:
        return {}
    return active


def write_active_run(payload: dict[str, Any]) -> None:
    write_json(active_run_path(), payload)


def is_bridge_child_session() -> bool:
    return os.environ.get("BRIDGE_CHILD_CLAUDE_SESSION", "").strip().lower() in {"1", "true", "yes"}


def last_bridge_packet_path() -> Path:
    return runtime_runs_root() / ".last_bridge_packet.json"


def load_last_bridge_packet() -> dict[str, Any]:
    path = last_bridge_packet_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def project_state_key(project_root: Path) -> str:
    import hashlib

    normalized = str(project_root).lower()
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{project_root.name}_{digest}"


def detect_run_id(payload: dict[str, Any]) -> str | None:
    for key in ("run_id", "control_run_id", "CLAUDE_CONTROL_RUN_ID", "BRIDGE_RUN_ID"):
        value = payload.get(key) or os.environ.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = _find_nested_run_id(payload)
    if nested:
        return nested
    active = read_active_run(payload)
    active_run_id = active.get("run_id")
    if isinstance(active_run_id, str) and active_run_id.strip():
        return active_run_id.strip()
    return None


def control_main_session_id(
    payload: dict[str, Any] | None = None,
    tool_input: dict[str, Any] | None = None,
    packet: dict[str, Any] | None = None,
) -> str | None:
    if is_bridge_child_session():
        for key in ("BRIDGE_MAIN_SESSION_ID", "CLAUDE_CONTROL_MAIN_SESSION_ID"):
            value = os.environ.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    binding = packet.get("binding", {}) if isinstance(packet, dict) else {}
    for value in (
        binding.get("main_session_id"),
        (tool_input or {}).get("main_session_id"),
        (payload or {}).get("main_session_id"),
        (payload or {}).get("session_id"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def control_binding_value(
    name: str,
    payload: dict[str, Any] | None = None,
    tool_input: dict[str, Any] | None = None,
    packet: dict[str, Any] | None = None,
    embedded: dict[str, Any] | None = None,
) -> Any:
    env_key = {
        "sub_session_id": "BRIDGE_SUB_SESSION_ID",
        "bridge_window_id": "BRIDGE_WINDOW_ID",
        "team_id": "BRIDGE_TEAM_ID",
        "task_id": "BRIDGE_TASK_ID",
    }.get(name)
    if is_bridge_child_session() and env_key:
        value = os.environ.get(env_key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    binding = packet.get("binding", {}) if isinstance(packet, dict) else {}
    for source in (payload or {}, tool_input or {}, embedded or {}, binding):
        if isinstance(source, dict):
            value = source.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if value:
                return value
    return None


def _find_nested_run_id(value: Any) -> str | None:
    if isinstance(value, dict):
        binding = value.get("binding")
        if isinstance(binding, dict):
            run_id = binding.get("run_id")
            if isinstance(run_id, str) and run_id.strip():
                return run_id.strip()
        for key in ("packet", "tool_input", "tool_response", "arguments", "event", "payload", "content", "result", "structured_output", "bridge_result"):
            nested = value.get(key)
            found = _find_nested_run_id(nested)
            if found:
                return found
        run_id = value.get("run_id")
        if isinstance(run_id, str) and run_id.strip():
            return run_id.strip()
        for nested in value.values():
            found = _find_nested_run_id(nested)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_nested_run_id(item)
            if found:
                return found
    if isinstance(value, str):
        parsed = parse_embedded_json(value)
        if parsed:
            return _find_nested_run_id(parsed)
    return None


def parse_embedded_json(text: Any) -> dict[str, Any]:
    if not isinstance(text, str):
        return {}
    s = text.strip()
    if not s:
        return {}
    if s.startswith("```"):
        lines = s.splitlines()
        if len(lines) >= 3:
            s = "\n".join(lines[1:-1]).strip()
    if not s.startswith("{"):
        return {}
    try:
        parsed = json.loads(s)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def invoke_runtime_event(event_payload: dict[str, Any], *, persist: bool = True) -> tuple[int, dict[str, Any], str]:
    cmd = [
        sys.executable,
        str(runtime_main()),
        "--control-root",
        str(control_root()),
        "--runtime-runs-root",
        str(runtime_runs_root()),
        "--event-json",
        json.dumps(event_payload, ensure_ascii=False),
    ]
    if persist:
        cmd.append("--persist")

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )

    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    parsed: dict[str, Any] = {}
    if stdout:
        try:
            obj = json.loads(stdout)
            if isinstance(obj, dict):
                parsed = obj
        except Exception:
            parsed = {"raw_stdout": stdout}
    if stderr and "runtime_stderr" not in parsed:
        parsed["runtime_stderr"] = stderr
    return proc.returncode, parsed, stderr


def simple_block(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def pretool_deny(reason: str) -> int:
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


def stop_block(reason: str) -> int:
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    return 0


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def emit_companion_event(kind: str, payload: dict[str, Any]) -> None:
    run_id = payload.get("run_id") or detect_run_id(payload)
    if not isinstance(run_id, str) or not run_id.strip():
        return
    run_root = runtime_runs_root() / run_id.strip()
    event_path = run_root / f"{kind}.jsonl"
    sequence = next_jsonl_sequence(event_path)
    record = {
        "timestamp": payload.get("timestamp") or now_iso(),
        "event_type": kind,
        **payload,
        "run_id": run_id.strip(),
        "sequence": payload.get("sequence") or sequence,
        "monotonic_index": payload.get("monotonic_index") or sequence,
    }
    append_jsonl(event_path, record)
    companion_path = run_root / "companion_events.jsonl"
    companion_sequence = next_jsonl_sequence(companion_path)
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


def compact_tool_target(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    keys = {
        "Read": ("file_path", "path"),
        "Edit": ("file_path", "path"),
        "Write": ("file_path", "path"),
        "MultiEdit": ("file_path", "path"),
        "Bash": ("command",),
        "Grep": ("pattern", "path"),
        "Glob": ("pattern", "path"),
        "LS": ("path",),
    }.get(tool_name, ("file_path", "path", "command", "pattern"))
    for key in keys:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def compact_tool_summary(tool_name: str, tool_input: dict[str, Any]) -> str:
    target = compact_tool_target(tool_name, tool_input)
    if target:
        return f"{tool_name} {target}"
    keys = ", ".join(sorted(str(key) for key in tool_input.keys())[:8])
    return f"{tool_name} input keys: {keys}" if keys else tool_name


def safe_input_preview(tool_input: dict[str, Any]) -> dict[str, Any]:
    preview: dict[str, Any] = {}
    key_map = {
        "file_path": "file_path",
        "path": "path",
        "command": "command",
        "pattern": "pattern",
        "glob": "glob",
        "description": "description",
    }
    for source_key, target_key in key_map.items():
        value = tool_input.get(source_key)
        if isinstance(value, str) and value.strip():
            preview[target_key] = redact_observer_text(value)[:500]
    return preview


def normalized_tool_input(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    preview = safe_input_preview(tool_input)
    if tool_name == "Bash" and "cwd" in tool_input:
        preview["cwd"] = str(tool_input.get("cwd"))[:500]
    return preview


def tool_file_refs(tool_name: str, tool_input: dict[str, Any], *, after: bool = False) -> list[dict[str, Any]]:
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
    refs: list[dict[str, Any]] = []
    for key in ("file_path", "path", "target"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            refs.append(file_ref_record(value, role=role, after=after))
    if tool_name == "Bash":
        cwd = tool_input.get("cwd") or tool_input.get("workdir")
        if isinstance(cwd, str) and cwd.strip():
            refs.append(file_ref_record(cwd, role="cwd", after=after))
    return dedupe_file_refs(refs)


def file_ref_record(path_text: str, *, role: str, after: bool = False) -> dict[str, Any]:
    path = str(path_text).strip()
    exists = Path(path).expanduser().exists() if path else False
    return {
        "path": path,
        "role": role,
        "exists_before": None if after else exists,
        "exists_after": exists if after else None,
    }


def dedupe_file_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for ref in refs:
        key = (ref.get("path"), ref.get("role"))
        if ref.get("path") and key not in seen:
            result.append(ref)
            seen.add(key)
    return result


def output_summary(tool_response: dict[str, Any], *, failed: bool) -> dict[str, Any]:
    stdout = str(tool_response.get("stdout") or tool_response.get("output") or "")
    stderr = str(tool_response.get("stderr") or "")
    exit_code = tool_response.get("exit_code")
    return {
        "stdout_lines": len(stdout.splitlines()) if stdout else 0,
        "stderr_lines": len(stderr.splitlines()) if stderr else 0,
        "truncated": len(stdout) > 1200 or len(stderr) > 1200,
        "notable": f"command exited {exit_code}" if exit_code is not None else ("tool failed" if failed else "tool completed"),
    }


def tool_start_record(run_id: str, tool_use_id: str | None) -> dict[str, Any]:
    if not run_id or not tool_use_id:
        return {}
    path = runtime_runs_root() / run_id / "tool_events.jsonl"
    for record in reversed(read_jsonl(path)):
        if record.get("tool_use_id") == tool_use_id and record.get("status") == "started":
            return record
    return {}


def duration_ms(started_at: Any, completed_at: str) -> int | None:
    if not isinstance(started_at, str) or not started_at:
        return None
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except Exception:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                records.append(parsed)
    except Exception:
        return []
    return records


def next_jsonl_sequence(path: Path) -> int:
    if not path.exists():
        return 1
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) + 1
    except Exception:
        return 1


def redact_observer_text(text: str) -> str:
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)(\s*[:=]\s*)(\S+)", r"\1\2<redacted>", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-<redacted>", text)
    return text
