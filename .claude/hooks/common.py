from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import hashlib
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


def session_observer_root() -> Path:
    configured = os.environ.get("BRIDGE_SESSION_OBSERVER_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return claude_root() / "runtime_state" / "session_observer"


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


def session_id_from_payload(payload: dict[str, Any] | None = None, tool_input: dict[str, Any] | None = None) -> str | None:
    for source in (payload or {}, tool_input or {}):
        if not isinstance(source, dict):
            continue
        for key in ("session_id", "sessionId", "main_session_id"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("session", "context", "transcript"):
            nested = source.get(key)
            if isinstance(nested, dict):
                found = session_id_from_payload(nested, None)
                if found:
                    return found
    for key in ("CLAUDE_SESSION_ID", "SESSION_ID"):
        value = os.environ.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def observer_binding(
    payload: dict[str, Any] | None = None,
    tool_input: dict[str, Any] | None = None,
    packet: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = payload or {}
    tool_input = tool_input or {}
    packet = packet if isinstance(packet, dict) else {}
    binding = packet.get("binding") if isinstance(packet.get("binding"), dict) else {}
    active = read_active_run(payload)
    session_id = session_id_from_payload(payload, tool_input)
    session_binding = read_latest_session_binding(session_id) if session_id else {}
    run_id = detect_run_id({**payload, "tool_input": tool_input, "packet": packet}) or ""
    if not run_id and isinstance(session_binding.get("run_id"), str):
        run_id = str(session_binding.get("run_id") or "")
    if run_id:
        run_binding_state = "bound_to_run"
    elif active.get("run_id"):
        run_id = str(active.get("run_id") or "")
        run_binding_state = "inferred"
    else:
        run_binding_state = "unbound"
    if is_bridge_child_session():
        session_kind = "bridge_child"
        binding_source = "env"
    elif session_binding.get("run_id"):
        session_kind = str(session_binding.get("session_kind") or "bridge_child")
        binding_source = "session_binding"
    elif run_binding_state in {"bound_to_run", "inferred"}:
        session_kind = "main_leader"
        binding_source = "active_run" if run_binding_state == "inferred" else "payload"
    else:
        session_kind = "direct_session"
        binding_source = "unbound"
    agent_type = (
        payload.get("agent_type")
        or tool_input.get("agent_type")
        or os.environ.get("BRIDGE_AGENT_TYPE")
        or session_binding.get("agent_type")
        or ("bridge-leader" if is_bridge_child_session() else "main-leader")
    )
    agent_id = payload.get("agent_id") or tool_input.get("agent_id") or os.environ.get("BRIDGE_AGENT_ID") or session_binding.get("agent_id") or agent_type
    teammate_id = (
        payload.get("teammate_id")
        or tool_input.get("teammate_id")
        or session_binding.get("teammate_id")
        or payload.get("agent_id")
        or tool_input.get("agent_id")
    )
    return {
        "session_kind": session_kind,
        "run_binding_state": run_binding_state,
        "session_id": session_id,
        "run_id": run_id or None,
        "main_session_id": control_main_session_id(payload, tool_input, packet) or active.get("main_session_id") or session_binding.get("main_session_id"),
        "sub_session_id": control_binding_value("sub_session_id", payload, tool_input, packet, binding) or session_binding.get("sub_session_id"),
        "bridge_window_id": control_binding_value("bridge_window_id", payload, tool_input, packet, binding) or session_binding.get("bridge_window_id"),
        "team_id": control_binding_value("team_id", payload, tool_input, packet, binding) or session_binding.get("team_id"),
        "task_id": control_binding_value("task_id", payload, tool_input, packet, binding) or session_binding.get("task_id"),
        "teammate_id": teammate_id or session_binding.get("teammate_id"),
        "agent_id": agent_id,
        "agent_type": agent_type,
        "display_name": payload.get("display_name") or tool_input.get("display_name") or teammate_id or session_binding.get("display_name") or agent_type,
        "binding_source": binding_source,
    }


def read_latest_session_binding(session_id: str | None) -> dict[str, Any]:
    if not session_id:
        return {}
    target = str(session_id).strip()
    if not target:
        return {}
    candidates = [session_observer_root() / "session_bindings.jsonl"]
    try:
        run_root = runtime_runs_root()
        if run_root.exists():
            candidates.extend(run_root.glob("*/session_bindings.jsonl"))
    except Exception:
        pass
    for path in candidates:
        binding = _latest_session_binding_from_file(path, target)
        if binding:
            return binding
    return {}


def _latest_session_binding_from_file(path: Path, session_id: str) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}
    for line in reversed(lines[-500:]):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if isinstance(record, dict) and str(record.get("session_id") or "").strip() == session_id:
            return record
    return {}


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


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _file_hash(path_text: str) -> str | None:
    if not path_text:
        return None
    try:
        path = Path(path_text).expanduser()
        if not path.exists() or not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()[:16]
    except Exception:
        return None


def _line_delta(old_text: str, new_text: str, edits: list[Any]) -> tuple[int | None, int | None]:
    if edits:
        added = 0
        removed = 0
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            old = str(edit.get("old_string") or "")
            new = str(edit.get("new_string") or "")
            removed += len(old.splitlines()) if old else 0
            added += len(new.splitlines()) if new else 0
        return added, removed
    if old_text or new_text:
        return len(new_text.splitlines()) if new_text else 0, len(old_text.splitlines()) if old_text else 0
    return None, None


def _safe_diff_preview(tool_name: str, tool_input: dict[str, Any], limit: int = 1200) -> str:
    if tool_name == "Write":
        text = str(tool_input.get("content") or "")
        return redact_observer_text(text[:limit])
    if tool_name == "Edit":
        old = str(tool_input.get("old_string") or "")
        new = str(tool_input.get("new_string") or "")
        preview = f"- {old[:400]}\n+ {new[:400]}"
        return redact_observer_text(preview[:limit])
    edits = tool_input.get("edits") if isinstance(tool_input.get("edits"), list) else []
    parts = []
    for edit in edits[:5]:
        if not isinstance(edit, dict):
            continue
        parts.append(f"- {str(edit.get('old_string') or '')[:160]}\n+ {str(edit.get('new_string') or '')[:160]}")
    return redact_observer_text("\n".join(parts)[:limit])


def _count_probable_files(output: str) -> int | None:
    if not output:
        return None
    seen = set()
    for line in output.splitlines():
        candidate = line.split(":", 1)[0].strip()
        if candidate:
            seen.add(candidate)
    return len(seen) if seen else None


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


def emit_session_observer_event(kind: str, payload: dict[str, Any]) -> None:
    root = session_observer_root()
    event_path = root / f"{kind}.jsonl"
    sequence = next_jsonl_sequence(event_path)
    record = {
        "timestamp": payload.get("timestamp") or now_iso(),
        "event_type": kind,
        **payload,
        "sequence": payload.get("sequence") or sequence,
        "monotonic_index": payload.get("monotonic_index") or sequence,
    }
    append_jsonl(event_path, record)
    if kind != "session_events":
        session_event_path = root / "session_events.jsonl"
        session_sequence = next_jsonl_sequence(session_event_path)
        append_jsonl(
            session_event_path,
            {
                **record,
                "session_sequence": session_sequence,
                "source_kind": kind,
                "source_file": f"{kind}.jsonl",
                "source_sequence": record["sequence"],
                "source_offset": record["sequence"],
            },
        )


def emit_observer_record(kind: str, payload: dict[str, Any]) -> None:
    run_id = payload.get("run_id")
    if isinstance(run_id, str) and run_id.strip():
        emit_companion_event(kind, payload)
    emit_session_observer_event(kind, payload)
    if kind == "tool_events":
        update_active_operation(payload)
        maybe_emit_session_binding(payload)


def maybe_emit_session_binding(payload: dict[str, Any]) -> None:
    session_id = payload.get("session_id")
    if not session_id:
        return
    record = {
        "timestamp": payload.get("timestamp") or now_iso(),
        "session_id": session_id,
        "run_id": payload.get("run_id"),
        "bridge_window_id": payload.get("bridge_window_id"),
        "team_id": payload.get("team_id"),
        "task_id": payload.get("task_id"),
        "teammate_id": payload.get("teammate_id"),
        "agent_type": payload.get("agent_type"),
        "display_name": payload.get("display_name") or payload.get("teammate_id") or payload.get("agent_type"),
        "binding_source": payload.get("binding_source") or "unknown",
        "session_kind": payload.get("session_kind") or "unknown",
        "run_binding_state": payload.get("run_binding_state") or "unknown",
    }
    if record["run_id"]:
        emit_companion_event("session_bindings", record)
    emit_session_observer_event("session_bindings", record)


def update_active_operation(payload: dict[str, Any]) -> None:
    status = str(payload.get("status") or "").strip().lower()
    if status not in {"started", "completed", "failed"}:
        return
    run_id = payload.get("run_id")
    if isinstance(run_id, str) and run_id.strip():
        path = runtime_runs_root() / run_id.strip() / "active_operations.json"
    else:
        path = session_observer_root() / "active_operations.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        state = {}
    if not isinstance(state, dict):
        state = {}
    teammates = state.get("teammates") if isinstance(state.get("teammates"), list) else []
    key = str(payload.get("teammate_id") or payload.get("agent_id") or payload.get("session_id") or "unknown")
    entry = None
    for item in teammates:
        if isinstance(item, dict) and str(item.get("teammate_id") or item.get("agent_id") or item.get("session_id")) == key:
            entry = item
            break
    if entry is None:
        entry = {"teammate_id": payload.get("teammate_id"), "agent_id": payload.get("agent_id"), "session_id": payload.get("session_id")}
        teammates.append(entry)
    entry.update(
        {
            "teammate_id": payload.get("teammate_id"),
            "agent_type": payload.get("agent_type"),
            "display_name": payload.get("display_name") or payload.get("agent_type"),
            "session_id": payload.get("session_id"),
            "bridge_window_id": payload.get("bridge_window_id"),
            "team_id": payload.get("team_id"),
            "task_id": payload.get("task_id"),
        }
    )
    tool_card = {
        "tool_use_id": payload.get("tool_use_id"),
        "tool_name": payload.get("tool_name"),
        "started_at": payload.get("started_at") or payload.get("timestamp"),
        "completed_at": payload.get("completed_at"),
        "target": payload.get("target"),
        "status": "running" if status == "started" else status,
        "summary": payload.get("summary"),
    }
    if status == "started":
        entry["active_tool"] = tool_card
    else:
        active = entry.get("active_tool") if isinstance(entry.get("active_tool"), dict) else {}
        if not active or active.get("tool_use_id") == payload.get("tool_use_id"):
            entry["active_tool"] = None
        entry["last_completed_tool"] = tool_card
    state.update({"run_id": run_id, "updated_at": payload.get("timestamp") or now_iso(), "teammates": teammates})
    write_json(path, state)


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


def bash_execution_soft_reminders(tool_name: str, tool_input: dict[str, Any], binding: dict[str, Any], *, after: bool = False, tool_response: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if tool_name != "Bash":
        return []
    actor = " ".join(
        str(binding.get(key) or "")
        for key in ("teammate_id", "agent_id", "agent_type", "display_name")
    ).casefold()
    if "executor" not in actor:
        return []
    command = str(tool_input.get("command") or "").strip()
    if not command:
        return []
    lowered = command.casefold()
    training_like = any(token in lowered for token in ("train", "finetune", "fine-tune", "accelerate", "torchrun", "deepspeed", "trl", "sft", "dpo", "opd", "evaluate", "eval"))
    gpu_related = any(token in lowered for token in ("cuda", "gpu", "nvidia", "torchrun", "deepspeed", "accelerate"))
    if not training_like and not gpu_related:
        return []
    smoke_like = any(token in lowered for token in ("smoke", "dry-run", "dry_run", "sanity", "quick", "debug", "--max_steps 1", "--max_steps=1", "--max-steps 1", "--max-steps=1"))
    has_gpu_probe = any(token in lowered for token in ("nvidia-smi", "gpustat", "torch.cuda.mem", "memory_allocated", "memory_reserved"))
    has_batch_hint = any(token in lowered for token in ("batch", "micro", "gradient_accumulation", "gradient-accumulation", "per_device", "per-device", "accumulation_steps"))
    has_manifest_hint = any(token in lowered for token in ("manifest", "run_manifest", "log_manifest"))
    reminders: list[dict[str, Any]] = []
    if smoke_like:
        if not has_gpu_probe:
            reminders.append(
                {
                    "level": "info",
                    "code": "executor_smoke_gpu_probe_recommended",
                    "message": "This looks like a smoke/debug execution. Do not kill it for low memory, but record a quick GPU/memory probe if accelerator shape matters.",
                }
            )
        return reminders
    if not has_gpu_probe:
        reminders.append(
            {
                "level": "warn",
                "code": "executor_formal_gpu_probe_missing",
                "message": "Formal-looking executor Bash should include or be paired with GPU memory monitoring evidence such as nvidia-smi/gpustat or framework memory stats.",
            }
        )
    if not has_batch_hint:
        reminders.append(
            {
                "level": "warn",
                "code": "executor_formal_batch_basis_missing",
                "message": "Formal-looking executor Bash should make batch/microbatch/gradient accumulation/effective batch basis explicit or reference the smoke-derived config.",
            }
        )
    if not has_manifest_hint:
        reminders.append(
            {
                "level": "warn",
                "code": "executor_log_manifest_reminder",
                "message": "Formal-looking executor Bash should create/update the log-folder manifest and report its path; filenames alone are not sufficient.",
            }
        )
    if after and isinstance(tool_response, dict):
        stdout = str(tool_response.get("stdout") or tool_response.get("output") or "")
        stderr = str(tool_response.get("stderr") or "")
        combined = f"{stdout}\n{stderr}".casefold()
        if "nvidia-smi" not in combined and "memory" not in combined and "cuda" not in combined:
            reminders.append(
                {
                    "level": "info",
                    "code": "executor_formal_output_lacks_memory_evidence",
                    "message": "Bash output does not show GPU memory evidence. If this was formal execution, follow with a non-destructive memory/log probe rather than treating the run as complete.",
                }
            )
    return reminders


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


def tool_detail_fields(tool_name: str, tool_input: dict[str, Any], tool_response: dict[str, Any] | None = None, *, failed: bool = False, after: bool = False) -> dict[str, Any]:
    tool_response = tool_response if isinstance(tool_response, dict) else {}
    if tool_name == "Read":
        output = str(tool_response.get("output") or tool_response.get("stdout") or "")
        details = {
            "read_options": {
                "offset": _int_or_none(tool_input.get("offset")),
                "limit": _int_or_none(tool_input.get("limit")),
            }
        }
        if after:
            details["output_summary"] = {
                "lines_returned": len(output.splitlines()) if output else None,
                "truncated": len(output) > 1200,
            }
        return details
    if tool_name in {"Edit", "Write", "MultiEdit"}:
        file_path = str(tool_input.get("file_path") or tool_input.get("path") or "").strip()
        operation = "multi_edit" if tool_name == "MultiEdit" else ("write" if tool_name == "Write" else "replace")
        edits = tool_input.get("edits") if isinstance(tool_input.get("edits"), list) else []
        old_text = str(tool_input.get("old_string") or "")
        new_text = str(tool_input.get("new_string") or tool_input.get("content") or "")
        added, removed = _line_delta(old_text, new_text, edits)
        return {
            "edit_summary": {
                "operation": operation,
                "hunks": len(edits) if edits else (1 if tool_name in {"Edit", "Write"} else 0),
                "lines_added": added,
                "lines_removed": removed,
                "before_hash": _file_hash(file_path) if not after else None,
                "after_hash": _file_hash(file_path) if after else None,
                "safe_diff_preview": _safe_diff_preview(tool_name, tool_input),
            }
        }
    if tool_name in {"Grep", "Glob"}:
        output = str(tool_response.get("output") or tool_response.get("stdout") or "")
        return {
            "search_summary": {
                "pattern_preview": redact_observer_text(str(tool_input.get("pattern") or tool_input.get("glob") or ""))[:300],
                "path": tool_input.get("path"),
                "files_matched": _count_probable_files(output),
                "matches_returned": len(output.splitlines()) if output else None,
            }
        }
    if tool_name == "Bash":
        stdout = str(tool_response.get("stdout") or tool_response.get("output") or "")
        stderr = str(tool_response.get("stderr") or "")
        return {
            "command_preview": redact_observer_text(str(tool_input.get("command") or ""))[:500],
            "cwd": tool_input.get("cwd") or tool_input.get("workdir") or os.getcwd(),
            "exit_code": tool_response.get("exit_code"),
            "stdout_tail": stdout[-1200:] if stdout else None,
            "stderr_tail": stderr[-1200:] if stderr else None,
            "spawned_processes": tool_response.get("spawned_processes") if isinstance(tool_response.get("spawned_processes"), list) else [],
            "long_running": bool(tool_response.get("long_running")),
            "failed": failed,
        }
    return {}


def tool_start_record(run_id: str, tool_use_id: str | None) -> dict[str, Any]:
    if not run_id or not tool_use_id:
        return {}
    path = runtime_runs_root() / run_id / "tool_events.jsonl"
    for record in reversed(read_jsonl(path)):
        if record.get("tool_use_id") == tool_use_id and record.get("status") == "started":
            return record
    return {}


def observer_tool_start_record(run_id: str | None, tool_use_id: str | None) -> dict[str, Any]:
    if not tool_use_id:
        return {}
    paths = []
    if isinstance(run_id, str) and run_id.strip():
        paths.append(runtime_runs_root() / run_id.strip() / "tool_events.jsonl")
    paths.append(session_observer_root() / "tool_events.jsonl")
    for path in paths:
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
