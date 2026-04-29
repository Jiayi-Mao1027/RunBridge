from __future__ import annotations

import hashlib
import json
import os
import shutil
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

TEAMMATE_AGENT_NAMES = {
    "chiefmate-a",
    "chiefmate-b",
    "preflight-initial",
    "refresher",
    "curator",
    "implementor",
    "rungater",
    "executor",
    "postrun",
    "anomaly-analyst-a",
    "anomaly-analyst-b",
}


def claude_cli_team_executor(execution_input: dict[str, Any]) -> dict[str, Any]:
    project_root = Path(os.environ.get("BRIDGE_PROJECT_ROOT") or Path.cwd()).resolve()
    packet = execution_input["packet"]
    prompt = _bridge_leader_prompt(packet, execution_input)
    prompt_path = _write_bridge_prompt_file(project_root, prompt, execution_input)
    teammate_names = _teammate_agent_names(packet)
    _ensure_project_agent_files(project_root, ["bridge-leader", *teammate_names])
    cmd = _claude_command_prefix() + [
        "-p",
        "--agent",
        "bridge-leader",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(BRIDGE_RESULT_SCHEMA, separators=(",", ":")),
        "--append-system-prompt",
        "Return structured JSON only.",
        "--add-dir",
        str(project_root),
    ]
    allowed_tools = _allowed_tools(packet, teammate_names)
    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])

    too_long = _command_too_long_for_windows(cmd)
    if too_long:
        return {
            "status": "failed",
            "reports": [],
            "artifact_refs": [],
            "evidence": {
                "command_length": too_long,
                "prompt_file": str(prompt_path),
                "platform": "windows",
            },
            "error_or_null": {"message": "claude cli command line would exceed Windows limit", "type": "CommandLineTooLong"},
            "cleanup_required": False,
        }

    proc = subprocess.run(
        cmd,
        cwd=str(project_root),
        input=prompt,
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
    if isinstance(payload, dict) and isinstance(payload.get("structured_output"), dict):
        payload = payload["structured_output"]
    elif isinstance(payload, dict) and isinstance(payload.get("result"), dict):
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
    if payload.get("status") not in {"succeeded", "failed", "partial", "partial_or_failed"}:
        return {
            "status": "failed",
            "reports": [{"summary": "claude cli bridge executor returned missing or invalid status"}],
            "artifact_refs": [],
            "evidence": {"stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]},
            "error_or_null": {"message": "claude cli bridge executor returned missing or invalid status"},
            "cleanup_required": False,
        }
    if not isinstance(payload.get("reports"), list):
        return {
            "status": "failed",
            "reports": [{"summary": "claude cli bridge executor returned missing or invalid reports"}],
            "artifact_refs": [],
            "evidence": {"stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]},
            "error_or_null": {"message": "claude cli bridge executor returned missing or invalid reports"},
            "cleanup_required": False,
        }
    if payload["status"] in {"succeeded", "partial", "partial_or_failed"} and not payload["reports"]:
        return {
            "status": "failed",
            "reports": [{"summary": "claude cli bridge executor returned no reports for non-failed status"}],
            "artifact_refs": [],
            "evidence": {"stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]},
            "error_or_null": {"message": "claude cli bridge executor returned no reports for non-failed status"},
            "cleanup_required": False,
        }
    if not isinstance(payload.get("artifact_refs"), list):
        payload["artifact_refs"] = []
    if "evidence" not in payload:
        payload["evidence"] = {"bridge_window_id": execution_input["bridge_window_id"]}
    if "error_or_null" not in payload:
        payload["error_or_null"] = None
    if "cleanup_required" not in payload:
        payload["cleanup_required"] = False
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


def _write_bridge_prompt_file(project_root: Path, prompt: str, execution_input: dict[str, Any]) -> Path:
    """Persist the prompt for audit while sending it to Claude through stdin."""
    prompt_path = _bridge_prompt_path(project_root, execution_input)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    return prompt_path


def _bridge_prompt_path(project_root: Path, execution_input: dict[str, Any]) -> Path:
    raw_key = "|".join(
        str(execution_input.get(key) or "")
        for key in ("run_id", "sub_session_id", "bridge_window_id", "team_id", "task_id")
    )
    digest = hashlib.sha1(raw_key.encode("utf-8", errors="replace")).hexdigest()[:16]
    run_id = _safe_path_component(str(execution_input.get("run_id") or "run"))
    return project_root / ".claude" / "runtime_state" / "bridge_prompts" / run_id / f"{digest}.md"


def _safe_path_component(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value)
    return (cleaned[:48].strip("._") or "run")


def _bridge_leader_system_prompt() -> str:
    prompt = _load_bridge_leader_agent_prompt()
    if prompt:
        return prompt + "\n\nReturn structured JSON only."
    return (
        "You are bridge-leader for exactly one bridge invocation window. "
        "You may inspect and modify only what the BridgePacket allows. "
        "You own the team/task execution for this window and must produce a report with evidence. "
        "Do not redefine frozen semantics or scope. Do not create multiple independent tasks. "
        "Return structured JSON only."
    )


def _load_bridge_leader_agent_prompt() -> str:
    agent_path = Path(__file__).resolve().parents[2] / "agents" / "bridge-leader.md"
    if not agent_path.exists():
        return ""
    text = agent_path.read_text(encoding="utf-8")
    _, body = _split_agent_markdown(text)
    return body


def _load_teammate_agents(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = {}
    teammate_specs = packet.get("team_spec", {}).get("teammate_specs", [])
    if not isinstance(teammate_specs, list):
        return loaded
    for teammate in teammate_specs:
        if not isinstance(teammate, dict):
            continue
        name = str(teammate.get("teammate_name") or "").strip()
        if name not in TEAMMATE_AGENT_NAMES:
            continue
        frontmatter, _body = _load_agent_markdown(name)
        responsibilities = teammate.get("responsibilities")
        if not isinstance(responsibilities, list):
            responsibilities = []
        role = str(teammate.get("role") or "bridge teammate")
        prompt = _compact_teammate_prompt(name, role, responsibilities)
        agent_config = {
            "description": frontmatter.get("description") or f"{name} teammate for this bridge packet",
            "prompt": prompt,
        }
        if frontmatter.get("model"):
            agent_config["model"] = frontmatter["model"]
        tools = [str(item) for item in teammate.get("allowed_tools", []) if str(item).strip()]
        if tools:
            agent_config["tools"] = tools
        loaded[name] = agent_config
    return loaded


def _teammate_agent_names(packet: dict[str, Any]) -> list[str]:
    names: list[str] = []
    teammate_specs = packet.get("team_spec", {}).get("teammate_specs", [])
    if not isinstance(teammate_specs, list):
        return names
    for teammate in teammate_specs:
        if not isinstance(teammate, dict):
            continue
        name = str(teammate.get("teammate_name") or "").strip()
        if name in TEAMMATE_AGENT_NAMES and name not in names:
            names.append(name)
    return names


def _ensure_project_agent_files(project_root: Path, names: list[str]) -> None:
    source_dir = Path(__file__).resolve().parents[2] / "agents"
    target_dir = project_root / ".claude" / "agents"
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        source = source_dir / f"{name}.md"
        if not source.exists():
            continue
        target = target_dir / source.name
        source_text = source.read_text(encoding="utf-8")
        if target.exists() and target.read_text(encoding="utf-8") == source_text:
            continue
        target.write_text(source_text, encoding="utf-8")


def _compact_teammate_prompt(name: str, role: str, responsibilities: list[Any]) -> str:
    responsibility_text = "; ".join(str(item) for item in responsibilities)
    agent_path = f".claude/agents/{name}.md"
    return (
        f"You are {name}, a {role} teammate for one bridge window. "
        f"Before acting, read {agent_path} and follow that static agent instruction. "
        "The BridgePacket assignment, tool boundary, and ownership boundary override any broader default behavior. "
        "Return concise evidence and findings to bridge-leader. "
        f"Packet responsibilities: {responsibility_text}"
    )


def _load_agent_markdown(name: str) -> tuple[dict[str, str], str]:
    agent_path = Path(__file__).resolve().parents[2] / "agents" / f"{name}.md"
    if not agent_path.exists():
        return {}, ""
    text = agent_path.read_text(encoding="utf-8")
    return _split_agent_markdown(text)


def _parse_tools(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _split_agent_markdown(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    frontmatter: dict[str, str] = {}
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                for raw_line in lines[1:index]:
                    key, sep, value = raw_line.partition(":")
                    if sep:
                        frontmatter[key.strip()] = value.strip()
                return frontmatter, "\n".join(lines[index + 1 :]).strip()
    return frontmatter, text.strip()


def _allowed_tools(packet: dict[str, Any], teammate_names: list[str] | None = None) -> list[str]:
    teammate_names = teammate_names if teammate_names is not None else _teammate_agent_names(packet)
    agent_tool = _agent_tool_name(teammate_names)
    configured = packet.get("allowed_tools")
    if isinstance(configured, list) and configured:
        tools = [str(item) for item in configured if str(item).strip()]
        tools = [agent_tool if item == "Agent" or item.startswith("Agent(") else item for item in tools]
        if teammate_names and agent_tool not in tools:
            return [agent_tool, *tools]
        return tools
    return [agent_tool, "Read", "Grep", "Glob", "LS", "Bash", "Edit", "Write"]


def _agent_tool_name(teammate_names: list[str]) -> str:
    if not teammate_names:
        return "Agent"
    allowed = [name for name in teammate_names if name in TEAMMATE_AGENT_NAMES]
    if not allowed:
        return "Agent"
    return f"Agent({','.join(allowed)})"


def _timeout_seconds(packet: dict[str, Any]) -> int:
    timeout_policy = packet.get("completion_contract", {}).get("timeout_policy") or {}
    hard_timeout = timeout_policy.get("hard_timeout_seconds")
    try:
        return max(30, int(hard_timeout))
    except Exception:
        return 3600


def _claude_command() -> str:
    return os.environ.get("BRIDGE_CLAUDE_COMMAND") or "claude"


def _claude_command_prefix() -> list[str]:
    configured = os.environ.get("BRIDGE_CLAUDE_COMMAND")
    if configured:
        return [configured]

    resolved = shutil.which("claude")
    if not resolved:
        return ["claude"]

    path = Path(resolved)
    exe_from_npm = path.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    if exe_from_npm.exists():
        return [str(exe_from_npm)]

    suffix = path.suffix.lower()
    if suffix in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(path)]
    if suffix == ".ps1":
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)]
    return [str(path)]


def _command_too_long_for_windows(cmd: list[str]) -> int | None:
    if os.name != "nt":
        return None
    command_length = sum(len(part) + 3 for part in cmd)
    return command_length if command_length > 30000 else None
