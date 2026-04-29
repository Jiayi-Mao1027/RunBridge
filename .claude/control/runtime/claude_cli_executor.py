from __future__ import annotations

import hashlib
import json
import os
import shlex
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
    required_agent_names = ["bridge-leader", *teammate_names]

    sync_result = _ensure_project_agent_files(project_root, required_agent_names)
    if sync_result.get("error_or_null"):
        return _failure(
            message="failed to sync required project agent files",
            error_type="AgentSyncFailed",
            evidence={
                "prompt_file": str(prompt_path),
                "agent_sync": sync_result,
            },
        )

    agent_models_result = _required_agent_models(required_agent_names)
    if agent_models_result.get("error_or_null"):
        return _failure(
            message="required agent model validation failed",
            error_type="AgentModelValidationFailed",
            evidence={
                "prompt_file": str(prompt_path),
                "agent_models": agent_models_result,
            },
        )

    agent_models: dict[str, str] = agent_models_result["models"]
    bridge_model = agent_models.get("bridge-leader") or os.environ.get("BRIDGE_FALLBACK_MODEL") or "gpt-main"

    cmd = (
        _claude_command_prefix()
        + _settings_args()
        + [
            "-p",
            "--agent",
            "bridge-leader",
            "--model",
            bridge_model,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(BRIDGE_RESULT_SCHEMA, separators=(",", ":")),
            "--append-system-prompt",
            "Return structured JSON only.",
            "--add-dir",
            str(project_root),
        ]
    )

    allowed_tools = _allowed_tools(packet, teammate_names)
    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])

    too_long = _command_too_long_for_windows(cmd)
    if too_long:
        return _failure(
            message="claude cli command line would exceed Windows limit",
            error_type="CommandLineTooLong",
            evidence={
                "command_length": too_long,
                "prompt_file": str(prompt_path),
                "platform": "windows",
                "agent_models": agent_models,
                "cmd_preview": _redact_cmd(cmd),
            },
        )

    env = _subprocess_env(
        bridge_model=bridge_model,
        teammate_names=teammate_names,
        agent_models=agent_models,
    )

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(project_root),
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_timeout_seconds(packet),
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return _failure(
            message="claude cli bridge executor timed out",
            error_type="ClaudeCliTimeout",
            evidence={
                "prompt_file": str(prompt_path),
                "timeout_seconds": _timeout_seconds(packet),
                "stdout": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
                "stderr": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
                "agent_models": agent_models,
                "cmd_preview": _redact_cmd(cmd),
            },
        )
    except Exception as exc:
        return _failure(
            message="claude cli bridge executor could not start",
            error_type=type(exc).__name__,
            evidence={
                "prompt_file": str(prompt_path),
                "exception": repr(exc),
                "agent_models": agent_models,
                "cmd_preview": _redact_cmd(cmd),
            },
        )

    if proc.returncode != 0:
        return _failure(
            message="claude cli bridge executor failed",
            error_type="ClaudeCliFailed",
            error_extra={"returncode": proc.returncode},
            evidence={
                "stderr": proc.stderr[-4000:],
                "stdout": proc.stdout[-4000:],
                "prompt_file": str(prompt_path),
                "agent_models": agent_models,
                "cmd_preview": _redact_cmd(cmd),
                "subagent_model_env": env.get("CLAUDE_CODE_SUBAGENT_MODEL"),
                "anthropic_model_env": env.get("ANTHROPIC_MODEL"),
                "default_sonnet_env": env.get("ANTHROPIC_DEFAULT_SONNET_MODEL"),
            },
        )

    payload_or_error = _parse_claude_payload(proc.stdout, proc.stderr)
    if payload_or_error.get("error_or_null"):
        payload_or_error["evidence"]["prompt_file"] = str(prompt_path)
        payload_or_error["evidence"]["agent_models"] = agent_models
        payload_or_error["evidence"]["cmd_preview"] = _redact_cmd(cmd)
        return payload_or_error

    payload = payload_or_error["payload"]
    normalized = _normalize_bridge_payload(payload, proc.stdout, proc.stderr)
    if normalized.get("error_or_null"):
        normalized["evidence"]["prompt_file"] = str(prompt_path)
        normalized["evidence"]["agent_models"] = agent_models
        normalized["evidence"]["cmd_preview"] = _redact_cmd(cmd)
        return normalized

    if "evidence" not in normalized or normalized["evidence"] is None:
        normalized["evidence"] = {}
    if isinstance(normalized["evidence"], dict):
        normalized["evidence"].setdefault("bridge_window_id", execution_input["bridge_window_id"])
        normalized["evidence"].setdefault("prompt_file", str(prompt_path))
        normalized["evidence"].setdefault("agent_models", agent_models)

    normalized.setdefault("artifact_refs", [])
    normalized.setdefault("error_or_null", None)
    normalized.setdefault("cleanup_required", False)
    return normalized


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


def _failure(
    *,
    message: str,
    error_type: str,
    evidence: dict[str, Any] | None = None,
    error_extra: dict[str, Any] | None = None,
    cleanup_required: bool = False,
) -> dict[str, Any]:
    error = {"message": message, "type": error_type}
    if error_extra:
        error.update(error_extra)
    return {
        "status": "failed",
        "reports": [],
        "artifact_refs": [],
        "evidence": evidence or {},
        "error_or_null": error,
        "cleanup_required": cleanup_required,
    }


def _bridge_leader_prompt(packet: dict[str, Any], execution_input: dict[str, Any]) -> str:
    binding = {
        k: execution_input[k]
        for k in ["run_id", "main_session_id", "sub_session_id", "bridge_window_id", "team_id", "task_id"]
        if k in execution_input
    }
    return (
        "Execute this one bridge-window task inside Claude Code. "
        "Stay inside the packet boundary. Return only JSON matching the requested schema.\n\n"
        f"Runtime binding:\n{json.dumps(binding, ensure_ascii=False, indent=2)}\n\n"
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


def _control_claude_dir() -> Path:
    # __file__ = .claude/control/runtime/claude_cli_executor.py
    return Path(__file__).resolve().parents[2]


def _source_agent_dir() -> Path:
    return _control_claude_dir() / "agents"


def _settings_args() -> list[str]:
    explicit = os.environ.get("BRIDGE_CLAUDE_SETTINGS")
    if explicit:
        return ["--settings", str(Path(explicit).expanduser().resolve())]

    default_settings = _control_claude_dir() / "settings.json"
    if default_settings.exists():
        return ["--settings", str(default_settings)]

    return []


def _ensure_project_agent_files(project_root: Path, names: list[str]) -> dict[str, Any]:
    source_dir = _source_agent_dir()
    target_dir = project_root / ".claude" / "agents"
    copied: list[str] = []
    missing: list[str] = []

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            source = source_dir / f"{name}.md"
            if not source.exists():
                missing.append(name)
                continue

            target = target_dir / source.name
            source_text = source.read_text(encoding="utf-8")

            if target.exists() and target.read_text(encoding="utf-8") == source_text:
                continue

            target.write_text(source_text, encoding="utf-8")
            copied.append(name)
    except Exception as exc:
        return {
            "copied": copied,
            "missing": missing,
            "error_or_null": {
                "type": type(exc).__name__,
                "message": repr(exc),
            },
        }

    if missing:
        return {
            "copied": copied,
            "missing": missing,
            "error_or_null": {
                "type": "MissingAgentFiles",
                "message": f"missing required agent files: {', '.join(missing)}",
            },
        }

    return {
        "copied": copied,
        "missing": [],
        "error_or_null": None,
    }


def _required_agent_models(names: list[str]) -> dict[str, Any]:
    models: dict[str, str] = {}
    missing_model: list[str] = []
    missing_file: list[str] = []
    invalid_model: dict[str, str] = {}

    allowed_models = _allowed_model_names()

    for name in names:
        frontmatter, _body = _load_agent_markdown(name)
        if not frontmatter:
            missing_file.append(name)
            continue

        model = str(frontmatter.get("model") or "").strip()
        if not model:
            missing_model.append(name)
            continue

        if allowed_models and model not in allowed_models:
            invalid_model[name] = model
            continue

        models[name] = model

    if missing_file or missing_model or invalid_model:
        return {
            "models": models,
            "missing_file_or_frontmatter": missing_file,
            "missing_model": missing_model,
            "invalid_model": invalid_model,
            "allowed_models": sorted(allowed_models) if allowed_models else None,
            "error_or_null": {
                "type": "RequiredAgentModelInvalid",
                "message": "one or more required agent markdown files lack a valid frontmatter model",
            },
        }

    return {
        "models": models,
        "missing_file_or_frontmatter": [],
        "missing_model": [],
        "invalid_model": {},
        "allowed_models": sorted(allowed_models) if allowed_models else None,
        "error_or_null": None,
    }


def _allowed_model_names() -> set[str]:
    raw = os.environ.get("BRIDGE_ALLOWED_MODELS", "gpt-main,sonnet-main")
    raw = raw.strip()
    if raw in {"", "*", "any", "ANY"}:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def _load_agent_markdown(name: str) -> tuple[dict[str, str], str]:
    agent_path = _source_agent_dir() / f"{name}.md"
    if not agent_path.exists():
        return {}, ""
    text = agent_path.read_text(encoding="utf-8-sig")
    return _split_agent_markdown(text)


def _split_agent_markdown(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    frontmatter: dict[str, str] = {}

    if not lines or lines[0].strip() != "---":
        return frontmatter, text.strip()

    end_index: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end_index = index
            break

    if end_index is None:
        return {}, text.strip()

    for raw_line in lines[1:end_index]:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition(":")
        if not sep:
            continue
        frontmatter[key.strip()] = value.strip().strip("'\"")

    return frontmatter, "\n".join(lines[end_index + 1 :]).strip()


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


def _allowed_tools(packet: dict[str, Any], teammate_names: list[str] | None = None) -> list[str]:
    teammate_names = teammate_names if teammate_names is not None else _teammate_agent_names(packet)
    agent_tool = _agent_tool_name(teammate_names)

    configured = packet.get("allowed_tools")
    if isinstance(configured, list) and configured:
        tools = [str(item).strip() for item in configured if str(item).strip()]
        normalized: list[str] = []

        for item in tools:
            if item == "Agent" or item.startswith("Agent("):
                if agent_tool not in normalized:
                    normalized.append(agent_tool)
            elif item not in normalized:
                normalized.append(item)

        if teammate_names and agent_tool not in normalized:
            normalized.insert(0, agent_tool)

        return normalized

    return [agent_tool, "Read", "Grep", "Glob", "LS", "Bash", "Edit", "Write"]


def _agent_tool_name(teammate_names: list[str]) -> str:
    if not teammate_names:
        return "Agent"

    allowed = [name for name in teammate_names if name in TEAMMATE_AGENT_NAMES]
    if not allowed:
        return "Agent"

    return f"Agent({','.join(allowed)})"


def _subprocess_env(
    *,
    bridge_model: str,
    teammate_names: list[str],
    agent_models: dict[str, str],
) -> dict[str, str]:
    env = os.environ.copy()

    # Force the bridge-leader process itself away from Claude Code's provider default.
    env.setdefault("ANTHROPIC_MODEL", bridge_model)
    env.setdefault("ANTHROPIC_DEFAULT_SONNET_MODEL", bridge_model)
    env.setdefault("ANTHROPIC_DEFAULT_HAIKU_MODEL", bridge_model)

    # If all teammates use one model, set the subagent override as a hard guard.
    # If teammates are heterogeneous, leave it unset so static per-agent frontmatter can decide.
    teammate_models = {
        agent_models[name]
        for name in teammate_names
        if name in agent_models and agent_models[name]
    }

    forced = os.environ.get("BRIDGE_FORCE_SUBAGENT_MODEL", "").strip().lower()
    explicit_subagent_model = os.environ.get("BRIDGE_SUBAGENT_MODEL", "").strip()

    if explicit_subagent_model:
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = explicit_subagent_model
    elif forced in {"1", "true", "yes"}:
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = bridge_model
    elif len(teammate_models) == 1:
        env.setdefault("CLAUDE_CODE_SUBAGENT_MODEL", next(iter(teammate_models)))

    return env


def _timeout_seconds(packet: dict[str, Any]) -> int:
    timeout_policy = packet.get("completion_contract", {}).get("timeout_policy") or {}
    hard_timeout = timeout_policy.get("hard_timeout_seconds")
    try:
        return max(30, int(hard_timeout))
    except Exception:
        return 3600


def _claude_command_prefix() -> list[str]:
    configured = os.environ.get("BRIDGE_CLAUDE_COMMAND")
    if configured:
        # Supports either:
        #   BRIDGE_CLAUDE_COMMAND=claude
        #   BRIDGE_CLAUDE_COMMAND="C:\path\to\claude.cmd"
        #   BRIDGE_CLAUDE_COMMAND="claude --some-wrapper-arg"
        try:
            parts = shlex.split(configured, posix=(os.name != "nt"))
            if parts:
                return parts
        except ValueError:
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


def _parse_claude_payload(stdout: str, stderr: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return _failure(
            message="claude cli bridge executor returned non-json output",
            error_type="ClaudeCliNonJsonOutput",
            evidence={
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
            },
        )

    if isinstance(payload, dict) and isinstance(payload.get("structured_output"), dict):
        payload = payload["structured_output"]
    elif isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        payload = payload["result"]

    if not isinstance(payload, dict):
        return _failure(
            message="claude cli bridge executor returned invalid payload",
            error_type="ClaudeCliInvalidPayload",
            evidence={
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
            },
        )

    return {"payload": payload, "error_or_null": None}


def _normalize_bridge_payload(payload: dict[str, Any], stdout: str, stderr: str) -> dict[str, Any]:
    status = payload.get("status")
    if status not in {"succeeded", "failed", "partial", "partial_or_failed"}:
        return _failure(
            message="claude cli bridge executor returned missing or invalid status",
            error_type="ClaudeCliInvalidStatus",
            evidence={
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
                "payload": payload,
            },
        )

    if not isinstance(payload.get("reports"), list):
        return _failure(
            message="claude cli bridge executor returned missing or invalid reports",
            error_type="ClaudeCliInvalidReports",
            evidence={
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
                "payload": payload,
            },
        )

    if status in {"succeeded", "partial", "partial_or_failed"} and not payload["reports"]:
        return _failure(
            message="claude cli bridge executor returned no reports for non-failed status",
            error_type="ClaudeCliMissingReports",
            evidence={
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
                "payload": payload,
            },
        )

    if not isinstance(payload.get("artifact_refs"), list):
        payload["artifact_refs"] = []

    if "evidence" not in payload:
        payload["evidence"] = {}

    if "error_or_null" not in payload:
        payload["error_or_null"] = None

    if "cleanup_required" not in payload:
        payload["cleanup_required"] = False

    return payload


def _redact_cmd(cmd: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False

    sensitive_flags = {
        "--api-key",
        "--auth-token",
        "--token",
        "--password",
    }

    for part in cmd:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue

        if part in sensitive_flags:
            redacted.append(part)
            redact_next = True
            continue

        lower = part.lower()
        if "token=" in lower or "api_key=" in lower or "apikey=" in lower or "password=" in lower:
            redacted.append("<redacted>")
            continue

        redacted.append(part)

    return redacted


# Kept for compatibility with older smoke tests or imports.
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
    _frontmatter, body = _load_agent_markdown("bridge-leader")
    return body


# Kept for compatibility with older dynamic-agent tests.
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

        agent_config: dict[str, Any] = {
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


def _parse_tools(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]