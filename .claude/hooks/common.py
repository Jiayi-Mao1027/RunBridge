from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_hook_input() -> dict[str, Any]:
    raw = sys.stdin.read()
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


def project_state_key(project_root: Path) -> str:
    import hashlib

    normalized = str(project_root).lower()
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{project_root.name}_{digest}"


def detect_run_id(payload: dict[str, Any]) -> str | None:
    for key in ("run_id", "control_run_id", "CLAUDE_CONTROL_RUN_ID"):
        value = payload.get(key) or os.environ.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = _find_nested_run_id(payload)
    if nested:
        return nested
    return None


def _find_nested_run_id(value: Any) -> str | None:
    if isinstance(value, dict):
        binding = value.get("binding")
        if isinstance(binding, dict):
            run_id = binding.get("run_id")
            if isinstance(run_id, str) and run_id.strip():
                return run_id.strip()
        for key in ("packet", "tool_input", "arguments", "event", "payload"):
            nested = value.get(key)
            found = _find_nested_run_id(nested)
            if found:
                return found
        run_id = value.get("run_id")
        if isinstance(run_id, str) and run_id.strip():
            return run_id.strip()
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

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
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
