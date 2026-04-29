from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


BRIDGE_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["succeeded", "failed", "partial", "partial_or_failed"]},
        "reports": {"type": "array", "items": {"type": "object"}},
        "artifact_refs": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": ["object", "null"]},
        "error_or_null": {"type": ["object", "null"]},
        "cleanup_required": {"type": "boolean"},
        "waiting": {"type": "boolean"},
        "wait_reason": {"type": "string"},
        "owned_process_refs": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["status", "reports", "artifact_refs", "evidence", "error_or_null", "cleanup_required"],
    "additionalProperties": True,
}


def claude_cli_team_executor(execution_input: dict[str, Any]) -> dict[str, Any]:
    project_root = Path(os.environ.get("BRIDGE_PROJECT_ROOT") or Path.cwd()).resolve()
    packet = execution_input["packet"]
    prompt = _bridge_leader_prompt(packet, execution_input)
    cmd = [
        _claude_command(),
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(BRIDGE_RESULT_SCHEMA, separators=(",", ":")),
        "--append-system-prompt",
        _bridge_leader_system_prompt(),
        "--add-dir",
        str(project_root),
    ]
    allowed_tools = _allowed_tools(packet)
    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])

    proc = subprocess.run(
        cmd,
        cwd=str(project_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=_timeout_seconds(packet),
    )
    if proc.returncode != 0:
        return {
            "status": "failed",
            "reports": [],
            "artifact_refs": [],
            "evidence": {"stderr": proc.stderr[-4000:], "stdout": proc.stdout[-4000:]},
            "error_or_null": {"message": "claude cli bridge executor failed", "returncode": proc.returncode},
            "cleanup_required": False,
        }
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "status": "failed",
            "reports": [],
            "artifact_refs": [],
            "evidence": {"stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]},
            "error_or_null": {"message": "claude cli bridge executor returned non-json output"},
            "cleanup_required": False,
        }
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        payload = payload["result"]
    if not isinstance(payload, dict):
        return {
            "status": "failed",
            "reports": [],
            "artifact_refs": [],
            "evidence": {"stdout": proc.stdout[-4000:]},
            "error_or_null": {"message": "claude cli bridge executor returned invalid payload"},
            "cleanup_required": False,
        }
    payload.setdefault("status", "succeeded")
    payload.setdefault("reports", [])
    payload.setdefault("artifact_refs", [])
    payload.setdefault("evidence", {"bridge_window_id": execution_input["bridge_window_id"]})
    payload.setdefault("error_or_null", None)
    payload.setdefault("cleanup_required", False)
    return payload


def simulated_team_executor(execution_input: dict[str, Any]) -> dict[str, Any]:
    packet = execution_input["packet"]
    task_spec = packet.get("task_spec", {})
    return {
        "status": "succeeded",
        "reports": [
            {
                "summary": f"Simulated completion for {task_spec.get('task_subject') or execution_input['task_id']}",
                "task_description": task_spec.get("task_description"),
            }
        ],
        "artifact_refs": [],
        "evidence": {
            "simulated": True,
            "bridge_window_id": execution_input["bridge_window_id"],
            "team_id": execution_input["team_id"],
            "task_id": execution_input["task_id"],
        },
        "error_or_null": None,
        "cleanup_required": False,
    }


def _bridge_leader_prompt(packet: dict[str, Any], execution_input: dict[str, Any]) -> str:
    return (
        "Execute this one bridge-window task inside Claude Code. "
        "Stay inside the packet boundary. Return only JSON matching the requested schema.\n\n"
        f"Runtime binding:\n{json.dumps({k: execution_input[k] for k in ['run_id', 'main_session_id', 'sub_session_id', 'bridge_window_id', 'team_id', 'task_id']}, ensure_ascii=False, indent=2)}\n\n"
        f"BridgePacket:\n{json.dumps(packet, ensure_ascii=False, indent=2)}"
    )


def _bridge_leader_system_prompt() -> str:
    return (
        "You are bridge-leader for exactly one bridge invocation window. "
        "You may inspect and modify only what the BridgePacket allows. "
        "You own the team/task execution for this window and must produce a report with evidence. "
        "Do not redefine frozen semantics or scope. Do not create multiple independent tasks. "
        "Return structured JSON only."
    )


def _allowed_tools(packet: dict[str, Any]) -> list[str]:
    configured = packet.get("allowed_tools")
    if isinstance(configured, list) and configured:
        return [str(item) for item in configured]
    return ["Read", "Grep", "Glob", "LS", "Bash", "Edit", "Write"]


def _timeout_seconds(packet: dict[str, Any]) -> int:
    timeout_policy = packet.get("completion_contract", {}).get("timeout_policy") or {}
    hard_timeout = timeout_policy.get("hard_timeout_seconds")
    try:
        return max(30, int(hard_timeout))
    except Exception:
        return 3600


def _claude_command() -> str:
    return os.environ.get("BRIDGE_CLAUDE_COMMAND") or "claude"
