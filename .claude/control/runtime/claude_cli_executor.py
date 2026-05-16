from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from output_guardrails import validate_bridge_result
from persist import append_jsonl, sanitize_json_value
from runtime_event_envelope import normalize_stream_record
from dispatch_contract import agent_tool_inputs


BRIDGE_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["succeeded", "failed", "partial", "partial_or_failed"]},
        "reports": {"type": "array", "items": {"type": "object"}},
        "artifact_refs": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "object"}]}},
        "evidence": {"anyOf": [{"type": "object"}, {"type": "null"}]},
        "error_or_null": {"anyOf": [{"type": "object"}, {"type": "null"}]},
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
    "chiefmate-c",
    "preflight-initial",
    "refresher",
    "curator",
    "implementor",
    "rungater",
    "executor",
    "postrun",
    "anomaly-analyst-a",
    "anomaly-analyst-b",
    "anomaly-analyst-c",
}

_SDK_STREAM_LOCK = threading.Lock()
_SDK_STREAM_MONOTONIC_INDEX = 0
_SDK_STREAM_PREVIEW_LIMIT = 1000


class ClaudeTmuxTerminalError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_type: str = "ClaudeTmuxTerminalError",
        capture: str = "",
        assistant_text: str = "",
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.capture = capture
        self.assistant_text = assistant_text


class ClaudeTmuxNoProgressTimeout(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        soft_timeout_seconds: int,
        progress_grace_seconds: int,
        latest_progress: dict[str, Any] | None = None,
        capture: str = "",
        assistant_text: str = "",
    ) -> None:
        super().__init__(message)
        self.soft_timeout_seconds = soft_timeout_seconds
        self.progress_grace_seconds = progress_grace_seconds
        self.latest_progress = latest_progress or {}
        self.capture = capture
        self.assistant_text = assistant_text


def claude_cli_team_executor(execution_input: dict[str, Any]) -> dict[str, Any]:
    project_root = Path(os.environ.get("BRIDGE_PROJECT_ROOT") or Path.cwd()).resolve()
    packet = _repair_mojibake_value(execution_input["packet"])

    prompt = _bridge_leader_prompt(packet, execution_input, project_root)
    prompt_path = _write_bridge_prompt_file(project_root, prompt, execution_input)

    teammate_names = _teammate_agent_names(packet)
    required_agent_names = ["bridge-leader", *teammate_names]

    sync_result = _ensure_project_agent_files(project_root, required_agent_names)
    if sync_result.get("error_or_null"):
        return _failure(
            message="failed to validate required control-plane agent files",
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

    bare_print_mode = _should_use_bare_print_mode(project_root)
    cmd = (
        _claude_command_prefix(project_root)
        + _settings_args(project_root)
        + (["--bare"] if bare_print_mode else [])
        + [
            "-p",
            "--agent",
            "bridge-leader",
            "--model",
            bridge_model,
            *_claude_print_stream_json_args(),
            "--include-hook-events",
            "--json-schema",
            json.dumps(BRIDGE_RESULT_SCHEMA, separators=(",", ":")),
            "--append-system-prompt",
            _bridge_append_system_prompt(packet),
            "--add-dir",
            str(project_root),
        ]
    )

    allowed_tools = _allowed_tools(packet, teammate_names)
    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])
    cmd.extend(["--", prompt])

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
        project_root=project_root,
    )
    _merge_settings_env_into_subprocess_env(cmd, env)
    _merge_claude_command_env_into_subprocess_env(env, project_root)
    _ensure_claude_api_key_alias(env)
    if bare_print_mode:
        env.setdefault("CLAUDE_CODE_SIMPLE", "1")
    _force_bridge_model_env(env, bridge_model)
    _bind_bridge_child_session_env(env, execution_input)

    try:
        timeout_seconds = _executor_timeout_seconds(packet)
        proc = _run_claude_streaming(
            cmd,
            project_root,
            env=env,
            timeout=timeout_seconds,
            execution_input=execution_input,
        )
    except subprocess.TimeoutExpired as exc:
        return _failure(
            message="claude cli bridge executor timed out",
            error_type="ClaudeCliTimeout",
            evidence={
                "prompt_file": str(prompt_path),
                "timeout_seconds": _executor_timeout_seconds(packet),
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
                "default_opus_env": env.get("ANTHROPIC_DEFAULT_OPUS_MODEL"),
                "default_sonnet_env": env.get("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                "default_haiku_env": env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
            },
        )

    payload_or_error = _parse_claude_payload(proc.stdout, proc.stderr)
    if payload_or_error.get("error_or_null"):
        _attach_cli_debug_evidence(payload_or_error, prompt_path, proc.stdout, proc.stderr)
        payload_or_error["evidence"]["prompt_file"] = str(prompt_path)
        payload_or_error["evidence"]["agent_models"] = agent_models
        payload_or_error["evidence"]["cmd_preview"] = _redact_cmd(cmd)
        return payload_or_error

    payload = payload_or_error["payload"]
    normalized = _normalize_bridge_payload(payload, proc.stdout, proc.stderr)
    normalized = _reconcile_observed_teammate_activity(normalized, project_root, execution_input)
    if normalized.get("error_or_null"):
        _attach_cli_debug_evidence(normalized, prompt_path, proc.stdout, proc.stderr, payload=payload)
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


def claude_tmux_team_executor(execution_input: dict[str, Any]) -> dict[str, Any]:
    """Interactive Claude Code bridge executor for custom-provider CLI setups."""
    project_root = Path(os.environ.get("BRIDGE_PROJECT_ROOT") or Path.cwd()).resolve()
    packet = _repair_mojibake_value(execution_input["packet"])

    if os.name == "nt":
        return _failure(
            message="tmux bridge executor is unavailable on Windows",
            error_type="ClaudeTmuxUnsupportedPlatform",
            evidence={"platform": os.name},
        )
    if not shutil.which("tmux"):
        return _failure(
            message="tmux bridge executor requires tmux",
            error_type="ClaudeTmuxMissing",
            evidence={"project_root": str(project_root)},
        )

    prompt = _bridge_leader_prompt(packet, execution_input, project_root)
    prompt_path = _write_bridge_prompt_file(project_root, prompt, execution_input)

    teammate_names = _teammate_agent_names(packet)
    required_agent_names = ["bridge-leader", *teammate_names]

    sync_result = _ensure_project_agent_files(project_root, required_agent_names)
    if sync_result.get("error_or_null"):
        return _failure(
            message="failed to validate required control-plane agent files",
            error_type="AgentSyncFailed",
            evidence={
                "prompt_file": str(prompt_path),
                "agent_sync": sync_result,
                "executor": "tmux",
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
                "executor": "tmux",
            },
        )

    agent_models: dict[str, str] = agent_models_result["models"]
    bridge_model = agent_models.get("bridge-leader") or os.environ.get("BRIDGE_FALLBACK_MODEL") or "gpt-main"
    cmd = (
        _claude_tty_command_prefix(project_root)
        + _settings_args(project_root)
        + [
            "--agent",
            "bridge-leader",
            "--model",
            bridge_model,
            "--append-system-prompt",
            _bridge_append_system_prompt(packet, compact=True),
            "--add-dir",
            str(project_root),
        ]
    )
    allowed_tools = _allowed_tools(packet, teammate_names)
    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])

    env = _subprocess_env(
        bridge_model=bridge_model,
        teammate_names=teammate_names,
        agent_models=agent_models,
        project_root=project_root,
    )
    _merge_settings_env_into_subprocess_env(cmd, env)
    _merge_claude_command_env_into_subprocess_env(env, project_root)
    _ensure_claude_api_key_alias(env)
    _force_bridge_model_env(env, bridge_model)
    _bind_bridge_child_session_env(env, execution_input)

    try:
        timeout_seconds = _executor_timeout_seconds(packet)
        tmux_result = _run_claude_tmux(
            cmd,
            project_root,
            env=env,
            timeout=timeout_seconds,
            execution_input=execution_input,
            prompt=prompt,
        )
    except ClaudeTmuxNoProgressTimeout as exc:
        return _failure(
            message="claude tmux bridge executor hit soft timeout without observer progress",
            error_type="ClaudeTmuxSoftTimeoutNoProgress",
            evidence={
                "prompt_file": str(prompt_path),
                "soft_timeout_seconds": exc.soft_timeout_seconds,
                "progress_grace_seconds": exc.progress_grace_seconds,
                "latest_observer_progress": exc.latest_progress,
                "assistant_text": exc.assistant_text[-4000:],
                "capture_tail": exc.capture[-4000:],
                "agent_models": agent_models,
                "cmd_preview": _redact_cmd(cmd),
                "executor": "tmux",
                "failure_classification": "bridge_transport_or_observer_progress_failure",
                "same_provider_assumption": "outer leader and bridge team use the same API provider; a bridge-only API failure should be treated as a system transport/config/runtime fault unless the outer leader is also failing",
            },
        )
    except ClaudeTmuxTerminalError as exc:
        return _failure(
            message="claude tmux bridge executor hit a terminal Claude API error",
            error_type=exc.error_type,
            evidence={
                "prompt_file": str(prompt_path),
                "terminal_error": str(exc),
                "assistant_text": exc.assistant_text[-4000:],
                "capture_tail": exc.capture[-4000:],
                "agent_models": agent_models,
                "cmd_preview": _redact_cmd(cmd),
                "executor": "tmux",
                "failure_classification": "bridge_transport_api_failure",
                "same_provider_assumption": "outer leader and bridge team use the same API provider; a bridge-only API failure should be treated as a system transport/config/runtime fault unless the outer leader is also failing",
            },
        )
    except subprocess.TimeoutExpired as exc:
        return _failure(
            message="claude tmux bridge executor timed out",
            error_type="ClaudeTmuxTimeout",
            evidence={
                "prompt_file": str(prompt_path),
                "timeout_seconds": _executor_timeout_seconds(packet),
                "stdout": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
                "stderr": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
                "agent_models": agent_models,
                "cmd_preview": _redact_cmd(cmd),
                "executor": "tmux",
            },
        )
    except Exception as exc:
        return _failure(
            message="claude tmux bridge executor could not start",
            error_type=type(exc).__name__,
            evidence={
                "prompt_file": str(prompt_path),
                "exception": repr(exc),
                "agent_models": agent_models,
                "cmd_preview": _redact_cmd(cmd),
                "executor": "tmux",
            },
        )

    assistant_text = tmux_result.get("assistant_text") or ""
    capture = tmux_result.get("capture") or ""
    payload = _parse_bridge_json_from_text(assistant_text)
    if not payload:
        result = _failure(
            message="claude tmux bridge executor returned no BridgeResult JSON",
            error_type="ClaudeTmuxNoBridgeJson",
            evidence={
                "prompt_file": str(prompt_path),
                "assistant_text": assistant_text[-4000:],
                "capture_tail": capture[-4000:],
                "agent_models": agent_models,
                "cmd_preview": _redact_cmd(cmd),
                "executor": "tmux",
            },
        )
        _attach_cli_debug_evidence(result, prompt_path, assistant_text, capture)
        return result

    normalized = _normalize_bridge_payload(payload, assistant_text, capture)
    normalized = _reconcile_observed_teammate_activity(normalized, project_root, execution_input)
    if normalized.get("error_or_null"):
        _attach_cli_debug_evidence(normalized, prompt_path, assistant_text, capture, payload=payload)
        normalized["evidence"]["prompt_file"] = str(prompt_path)
        normalized["evidence"]["agent_models"] = agent_models
        normalized["evidence"]["cmd_preview"] = _redact_cmd(cmd)
        normalized["evidence"]["executor"] = "tmux"
        return normalized

    if "evidence" not in normalized or normalized["evidence"] is None:
        normalized["evidence"] = {}
    if isinstance(normalized["evidence"], dict):
        normalized["evidence"].setdefault("bridge_window_id", execution_input["bridge_window_id"])
        normalized["evidence"].setdefault("prompt_file", str(prompt_path))
        normalized["evidence"].setdefault("agent_models", agent_models)
        normalized["evidence"].setdefault("executor", "tmux")

    normalized.setdefault("artifact_refs", [])
    normalized.setdefault("error_or_null", None)
    normalized.setdefault("cleanup_required", False)
    return normalized


def simulated_team_executor(execution_input: dict[str, Any]) -> dict[str, Any]:
    packet = execution_input["packet"]
    task_spec = packet.get("task_spec", {})
    completion_contract = packet.get("completion_contract", {}) if isinstance(packet.get("completion_contract"), dict) else {}
    required_artifacts = completion_contract.get("required_artifacts")
    artifact_refs = [str(item) for item in required_artifacts] if isinstance(required_artifacts, list) else []
    manifest_required = "log_manifest" in artifact_refs
    configured_manifest_fields = completion_contract.get("manifest_required_fields")
    manifest_fields = [str(item) for item in configured_manifest_fields] if isinstance(configured_manifest_fields, list) else []
    if manifest_required and not manifest_fields:
        manifest_fields = [
            "run_id",
            "bridge_window_id",
            "task_id",
            "command",
            "cwd",
            "batchbasis",
            "gpu_id",
            "memory observed",
            "model",
            "dataset",
            "method",
        ]
    manifest_defaults = {
        "run_id": execution_input.get("run_id"),
        "bridge_window_id": execution_input.get("bridge_window_id"),
        "task_id": execution_input.get("task_id"),
        "command": "simulated command",
        "cwd": "simulated cwd",
        "batchbasis": "simulated batch basis",
        "gpu_id": "not_applicable",
        "gpu_id_or_device_ids": "not_applicable",
        "memory observed": "not_applicable",
        "smoke_memory_observed": "not_applicable",
        "warmup_memory_observed": "not_applicable",
        "formal_memory_observed": "not_applicable",
        "model": "simulated model",
        "model_or_model_family": "simulated model",
        "dataset": "simulated dataset",
        "dataset_name_split_source": "simulated dataset",
        "method": "simulated method",
        "method_or_objective": "simulated method",
        "terminal_status": "succeeded",
    }
    manifest_checklist = {
        field: manifest_defaults.get(field, "not_applicable: simulated executor")
        for field in manifest_fields
    } if manifest_required else {}
    coverage_items = task_spec.get("instruction_coverage_checklist") if isinstance(task_spec.get("instruction_coverage_checklist"), list) else []
    instruction_coverage = {str(item): "completed" for item in coverage_items if str(item)}
    if not instruction_coverage:
        instruction_coverage = {"simulated completion": "completed"}
    return {
        "status": "succeeded",
        "reports": [
            {
                "summary": f"Simulated completion for {task_spec.get('task_subject') or execution_input['task_id']}",
                "task_description": task_spec.get("task_description"),
                "instruction_coverage": instruction_coverage,
                "semantic_identity_resolution": {
                    "disposition": "not_applicable",
                    "basis": "simulated executor",
                },
                "evidence_refs": [f"task:{execution_input['task_id']}"],
                **({"manifest required fields checklist": manifest_checklist} if manifest_required else {}),
            }
        ],
        "artifact_refs": artifact_refs,
        "evidence": {
            "simulated": True,
            "bridge_window_id": execution_input["bridge_window_id"],
            "team_id": execution_input["team_id"],
            "task_id": execution_input["task_id"],
            "artifact_refs": artifact_refs,
            **({"manifest_required_fields_checklist": manifest_checklist} if manifest_required else {}),
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


def _bridge_leader_prompt(packet: dict[str, Any], execution_input: dict[str, Any], project_root: Path) -> str:
    packet = _repair_mojibake_value(packet)
    binding = {
        k: execution_input[k]
        for k in ["run_id", "main_session_id", "sub_session_id", "bridge_window_id", "team_id", "task_id"]
        if k in execution_input
    }
    agent_inputs = _agent_tool_inputs_for_prompt(packet)
    return (
        "Execute this one bridge-window task inside Claude Code. "
        "Stay inside the packet boundary. Return only JSON matching the requested schema.\n\n"
        f"Project root boundary:\n{project_root}\n"
        "Treat this path as the repository root for this bridge invocation. "
        "All relative paths in the packet are relative to this directory. "
        "Do not read or write outside this directory, even if Claude Code exposes a parent Git workspace. "
        "When dispatching teammates, include this same project-root boundary in each Agent message.\n\n"
        "Tool compatibility guard: when using Read, omit optional parameters you do not need. "
        "Never pass an empty string for pages; either omit pages entirely or use a concrete range like 1-5.\n\n"
        "Runtime-owned Agent dispatch inputs: these are the only legal Agent tool payloads. "
        "Copy one object exactly when dispatching that teammate; do not reconstruct, splice, translate, suffix, "
        "or add mechanical fields.\n"
        f"{json.dumps(agent_inputs, ensure_ascii=False, indent=2)}\n\n"
        "Teammate dispatch guard: do not compose Agent payloads yourself. Use only the runtime-owned Agent "
        "dispatch inputs above. Do not pass audit metadata, routing hints, model, isolation, "
        "run_in_background, or any other wrapper/mechanical field to the Agent tool. "
        "Do not ask teammates to read .claude/runtime_state/bridge_prompts; "
        "that prompt artifact is for audit only.\n\n"
        f"{_subagent_model_guard(packet)}\n\n"
        "Missing teammate retry guard: before returning partial or partial_or_failed for a missing teammate report "
        "caused by transient API/transport/no-output failure, you may make only bounded packet-bound collection "
        "or re-dispatch attempts while this bridge window is live and the packet boundary, allowed tools, and "
        "timeout still permit it. Do not consume BridgePacket.retry_policies.teammate_report_missing as a "
        "same-window retry loop; that retry policy is runtime-owned after a terminal BridgeResult. Record any "
        "same-window attempt and outcome in evidence, then return the structured result without broadening scope.\n\n"
        "Observer reconciliation guard: if an Agent call appears to complete but no usable teammate report is visible, "
        "inspect same-window observer evidence when available (tool_events.jsonl, session_events.jsonl, session_bindings.jsonl). "
        "If those records show teammate tool activity, include observer refs as diagnostic evidence only; do not treat "
        "observer streams as proof of task completion or as authority to override BridgeResult status/error classification.\n\n"
        "BridgeResult report guard: every reports[i].instruction_coverage must be a JSON object that maps each "
        "coverage checklist item, or a clearly named subitem, to one disposition string: completed, deferred, "
        "blocked, or escalated. Never use bucket keys such as completed: [..] or blocked: [..]. "
        "If a teammate failed or output is missing, map the affected coverage items to blocked or escalated with "
        "the reason in summary/evidence. If any item is completed, include a non-empty evidence_refs list or "
        "non-empty evidence object in that same report; prefer concrete file, runtime event, tool observation, "
        "or teammate report refs.\n\n"
        "L4 execute wait guard: if packet.target_phase is l4_execute and executor launches an owned long-running "
        "process, keep the bridge window alive until that process reaches a terminal state and postrun has audited "
        "the terminal logs/artifacts. TeamIdle is only waiting/progress evidence. Do not return status partial, "
        "partial_or_failed, or succeeded while an owned process is still running; record progress and continue "
        "waiting or polling inside this bridge window.\n\n"
        f"Runtime binding:\n{json.dumps(binding, ensure_ascii=False, indent=2)}\n\n"
        f"BridgePacket:\n{json.dumps(_packet_for_bridge_prompt(packet, agent_inputs), ensure_ascii=False, indent=2)}"
    )


def _bridge_append_system_prompt(packet: dict[str, Any], *, compact: bool = False) -> str:
    return_format = (
        "Return one compact JSON object only. Do not wrap it in Markdown."
        if compact
        else "Return structured JSON only."
    )
    return f"{return_format}\n\n{_subagent_model_guard(packet)}"


def _subagent_model_guard_legacy(packet: dict[str, Any]) -> str:
    return (
        "Subagent dispatch guard: use only the Agent tool input fields named by "
        "task_team_mapping.teammate_assignments[*].agent_dispatch.allowed_input_keys, with values copied from "
        "that system-generated agent_dispatch. The selected .claude/agents/<subagent_type>.md frontmatter owns "
        "model routing outside the Agent tool input; do not choose a model alias yourself. Hooks normalize "
        "Claude Code's default Agent schema carrier and deny non-default aliases or other model overrides. "
        "The subagent_type value is a machine identifier: copy it exactly as ASCII from agent_dispatch, never "
        "translate it, add suffixes, or combine it with localized action words such as curator办理 or refresher办理. "
        "Do not set isolation, "
        "run_in_background, tool_name, allowed_input_keys, or any other mechanical override yourself; hooks "
        "tolerate known Claude wrapper auto-fields but deny agent-authored payloads that differ from the "
        "dispatch contract."
    )


def _subagent_model_guard(packet: dict[str, Any]) -> str:
    return (
        "Subagent dispatch guard: use only the runtime-owned Agent dispatch input objects printed above. "
        "The subagent_type value is a machine identifier: copy it exactly, never translate it, add suffixes, "
        "or combine it with localized action words. Do not add model, isolation, run_in_background, tool_name, "
        "routing hints, or any other mechanical override. If all expected teammate reports "
        "are already available, stop dispatching Agents and return the BridgeResult from existing reports."
    )


def _agent_tool_inputs_for_prompt(packet: dict[str, Any]) -> dict[str, dict[str, str]]:
    contract = packet.get("dispatch_contract") if isinstance(packet, dict) else None
    if not isinstance(contract, dict):
        return {}
    return agent_tool_inputs(contract)


def _packet_for_bridge_prompt(packet: dict[str, Any], agent_inputs: dict[str, dict[str, str]]) -> dict[str, Any]:
    prompt_packet = deepcopy(packet)
    prompt_packet.pop("dispatch_contract", None)
    mapping = prompt_packet.get("task_team_mapping")
    assignments = mapping.get("teammate_assignments") if isinstance(mapping, dict) else None
    if isinstance(assignments, list):
        for assignment in assignments:
            if isinstance(assignment, dict):
                assignment.pop("agent_dispatch", None)
    prompt_packet["runtime_owned_agent_dispatch_inputs"] = deepcopy(agent_inputs)
    return prompt_packet


def _write_bridge_prompt_file(project_root: Path, prompt: str, execution_input: dict[str, Any]) -> Path:
    """Persist the prompt for audit while sending it to Claude through stdin."""
    prompt_path = _bridge_prompt_path(project_root, execution_input)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    return prompt_path


def _run_claude_streaming(
    cmd: list[str],
    project_root: Path,
    *,
    env: dict[str, str],
    timeout: int | None,
    execution_input: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    sequence = 0
    sequence_lock = threading.Lock()

    def next_sequence() -> int:
        nonlocal sequence
        with sequence_lock:
            sequence += 1
            return sequence

    _emit_sdk_stream_event(
        project_root,
        execution_input,
        "sdk_stream_started",
        {
            "cmd_preview": _redact_cmd(cmd),
            "settings_diagnostics": _settings_diagnostics(cmd, env, project_root=project_root),
        },
        sequence=next_sequence(),
    )

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(project_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except Exception:
        _emit_sdk_stream_event(
            project_root,
            execution_input,
            "sdk_stream_error",
            {"message": "claude cli bridge executor could not start"},
            status="failed",
            sequence=next_sequence(),
        )
        raise

    def read_stdout() -> None:
        for line in proc.stdout or []:
            stdout_parts.append(line)
            parsed = _parse_json_object_text(line)
            payload = parsed if parsed is not None else {"text": line}
            _emit_sdk_stream_event(
                project_root,
                execution_input,
                _sdk_stream_event_type(payload),
                payload,
                sequence=next_sequence(),
            )

    def read_stderr() -> None:
        for line in proc.stderr or []:
            stderr_parts.append(line)
            _emit_sdk_stream_event(
                project_root,
                execution_input,
                "sdk_stream_stderr",
                {"text": line},
                sequence=next_sequence(),
            )

    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    try:
        returncode = proc.wait(timeout=timeout)
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        _emit_sdk_stream_event(
            project_root,
            execution_input,
            "sdk_stream_interrupted",
            {"message": "claude cli bridge executor interrupted by user"},
            status="failed",
            sequence=next_sequence(),
        )
        raise
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
        _emit_sdk_stream_event(
            project_root,
            execution_input,
            "sdk_stream_timeout",
            {"timeout_seconds": timeout},
            status="failed",
            sequence=next_sequence(),
        )
        raise subprocess.TimeoutExpired(exc.cmd, exc.timeout, output=stdout, stderr=stderr) from exc

    stdout_thread.join(timeout=2)
    stderr_thread.join(timeout=2)
    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)
    _emit_sdk_stream_event(
        project_root,
        execution_input,
        "sdk_stream_final",
        {"returncode": returncode},
        status="completed" if returncode == 0 else "failed",
        sequence=next_sequence(),
    )
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


def _run_claude_tmux(
    cmd: list[str],
    project_root: Path,
    *,
    env: dict[str, str],
    timeout: int | None,
    execution_input: dict[str, Any],
    prompt: str,
) -> dict[str, str]:
    session_name = _tmux_bridge_session_name(execution_input)
    sequence = 0

    def next_sequence() -> int:
        nonlocal sequence
        sequence += 1
        return sequence

    _emit_sdk_stream_event(
        project_root,
        execution_input,
        "sdk_stream_started",
        {
            "adapter": "claude-tmux-bridge",
            "tmux_session": session_name,
            "cmd_preview": _redact_cmd(cmd),
            "settings_diagnostics": _settings_diagnostics(cmd, env, project_root=project_root),
        },
        sequence=next_sequence(),
    )

    capture = ""
    assistant_text = ""
    try:
        _tmux_run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session_name,
                "-x",
                "1000",
                "-y",
                "60",
                _tmux_launch_command(cmd, project_root, env),
            ]
        )
        _wait_for_tmux_ready(session_name, timeout=45)
        _tmux_paste_prompt(session_name, prompt)
        started_at = time.time()
        packet = execution_input.get("packet", {})
        watchdog_enabled = _tmux_no_progress_watchdog_enabled(packet)
        soft_timeout = _soft_timeout_seconds(packet, hard_timeout_seconds=timeout) if watchdog_enabled else None
        progress_grace_seconds = _tmux_no_progress_grace_seconds()
        latest_progress = _latest_observer_progress(project_root, execution_input)
        latest_progress_epoch = max(started_at, _observer_progress_epoch(latest_progress) or started_at)
        deadline = (time.time() + timeout) if timeout is not None else None
        while deadline is None or time.time() < deadline:
            capture = _tmux_capture(session_name)
            assistant_text = _tmux_assistant_text(capture, prompt)
            observed_progress = _latest_observer_progress(project_root, execution_input)
            observed_progress_epoch = _observer_progress_epoch(observed_progress)
            if observed_progress_epoch is not None and observed_progress_epoch > latest_progress_epoch:
                latest_progress = observed_progress
                latest_progress_epoch = observed_progress_epoch
            payload = _parse_bridge_json_from_text(assistant_text)
            if isinstance(payload, dict) and payload.get("status") in {"succeeded", "failed", "partial", "partial_or_failed"}:
                _emit_sdk_stream_event(
                    project_root,
                    execution_input,
                    "sdk_stream_assistant_text",
                    {"type": "assistant", "text": assistant_text, "adapter": "claude-tmux-bridge"},
                    sequence=next_sequence(),
                )
                _emit_sdk_stream_event(
                    project_root,
                    execution_input,
                    "sdk_stream_final_result",
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": assistant_text,
                        "adapter": "claude-tmux-bridge",
                    },
                    status="completed",
                    sequence=next_sequence(),
                )
                _emit_sdk_stream_event(
                    project_root,
                    execution_input,
                    "sdk_stream_final",
                    {"returncode": 0, "adapter": "claude-tmux-bridge"},
                    status="completed",
                    sequence=next_sequence(),
                )
                return {"assistant_text": assistant_text, "capture": capture}
            terminal_error = _tmux_terminal_error(capture)
            if terminal_error:
                _emit_sdk_stream_event(
                    project_root,
                    execution_input,
                    "sdk_stream_error",
                    {
                        "message": terminal_error["message"],
                        "error_type": terminal_error["type"],
                        "adapter": "claude-tmux-bridge",
                    },
                    status="failed",
                    sequence=next_sequence(),
                )
                _emit_sdk_stream_event(
                    project_root,
                    execution_input,
                    "sdk_stream_final",
                    {"returncode": 1, "adapter": "claude-tmux-bridge"},
                    status="failed",
                    sequence=next_sequence(),
                )
                raise ClaudeTmuxTerminalError(
                    terminal_error["message"],
                    error_type=terminal_error["type"],
                    capture=capture,
                    assistant_text=assistant_text,
                )
            now = time.time()
            if soft_timeout is not None and now >= started_at + soft_timeout and now >= latest_progress_epoch + progress_grace_seconds:
                progress_age_seconds = max(0, int(now - latest_progress_epoch))
                progress_payload = {
                    "timeout_seconds": timeout,
                    "soft_timeout_seconds": soft_timeout,
                    "progress_grace_seconds": progress_grace_seconds,
                    "latest_progress_age_seconds": progress_age_seconds,
                    "latest_observer_progress": latest_progress,
                    "adapter": "claude-tmux-bridge",
                }
                _emit_sdk_stream_event(
                    project_root,
                    execution_input,
                    "sdk_stream_timeout",
                    progress_payload,
                    status="failed",
                    sequence=next_sequence(),
                )
                _emit_sdk_stream_event(
                    project_root,
                    execution_input,
                    "sdk_stream_final",
                    {"returncode": 1, "adapter": "claude-tmux-bridge"},
                    status="failed",
                    sequence=next_sequence(),
                )
                raise ClaudeTmuxNoProgressTimeout(
                    "soft timeout elapsed without new run-scoped observer progress",
                    soft_timeout_seconds=soft_timeout,
                    progress_grace_seconds=progress_grace_seconds,
                    latest_progress=latest_progress,
                    capture=capture,
                    assistant_text=assistant_text,
                )
            time.sleep(2)
        _emit_sdk_stream_event(
            project_root,
            execution_input,
            "sdk_stream_timeout",
            {"timeout_seconds": timeout, "adapter": "claude-tmux-bridge"},
            status="failed",
            sequence=next_sequence(),
        )
        raise subprocess.TimeoutExpired(cmd, timeout, output=assistant_text or capture, stderr="")
    finally:
        _tmux_run(["tmux", "kill-session", "-t", session_name], check=False)


def _emit_sdk_stream_event(
    project_root: Path,
    execution_input: dict[str, Any],
    event_type: str,
    payload: dict[str, Any],
    *,
    status: str = "streaming",
    sequence: int | None = None,
) -> None:
    global _SDK_STREAM_MONOTONIC_INDEX
    record = {
        "timestamp": _now_iso(),
        "event_type": event_type,
        "stream_source": "sdk",
        "raw_stream_event_type": str(payload.get("type") or event_type) if isinstance(payload, dict) else event_type,
        "run_id": execution_input.get("run_id"),
        "main_session_id": execution_input.get("main_session_id"),
        "sub_session_id": execution_input.get("sub_session_id"),
        "bridge_window_id": execution_input.get("bridge_window_id"),
        "team_id": execution_input.get("team_id"),
        "task_id": execution_input.get("task_id"),
        "session_id": execution_input.get("sub_session_id") or execution_input.get("main_session_id"),
        "agent_id": "bridge-leader",
        "agent_type": "bridge-leader",
        "status": status,
        "message_preview": _sdk_message_preview(payload),
        "text_delta": _sdk_text_delta(payload),
        "input_json_delta": _sdk_input_json_delta(payload),
        "payload_keys": _sdk_payload_keys(payload),
        "sequence": sequence,
        "monotonic_index": None,
    }
    record.update(_sdk_compact_tool_fields(payload))
    if "cmd_preview" in payload:
        record["cmd_preview"] = sanitize_json_value(payload.get("cmd_preview"))
    if "adapter" in payload:
        record["adapter"] = sanitize_json_value(payload.get("adapter"))
    if "tmux_session" in payload:
        record["tmux_session"] = sanitize_json_value(payload.get("tmux_session"))
    if "settings_diagnostics" in payload:
        record["settings_diagnostics"] = sanitize_json_value(payload.get("settings_diagnostics"))
    if "returncode" in payload:
        record["returncode"] = payload.get("returncode")
    if "timeout_seconds" in payload:
        record["timeout_seconds"] = payload.get("timeout_seconds")
    if "soft_timeout_seconds" in payload:
        record["soft_timeout_seconds"] = payload.get("soft_timeout_seconds")
    if "progress_grace_seconds" in payload:
        record["progress_grace_seconds"] = payload.get("progress_grace_seconds")
    if "latest_progress_age_seconds" in payload:
        record["latest_progress_age_seconds"] = payload.get("latest_progress_age_seconds")
    if "latest_observer_progress" in payload:
        record["latest_observer_progress"] = sanitize_json_value(payload.get("latest_observer_progress"))

    with _SDK_STREAM_LOCK:
        _SDK_STREAM_MONOTONIC_INDEX += 1
        record["monotonic_index"] = _SDK_STREAM_MONOTONIC_INDEX
        if record["sequence"] is None:
            record["sequence"] = record["monotonic_index"]
        record["runtime_event"] = normalize_stream_record(
            record,
            source="cli",
            authority="observed",
            event_kind=event_type,
            seq=record.get("sequence"),
            payload_ref="sdk_stream_events.jsonl",
        )
        for path in _sdk_stream_event_paths(project_root, execution_input):
            append_jsonl(path, record)


def _sdk_stream_event_paths(project_root: Path, execution_input: dict[str, Any]) -> list[Path]:
    run_id = _safe_path_component(str(execution_input.get("run_id") or "run"))
    return [
        _control_claude_dir()
        / "runtime_state"
        / "projects"
        / _project_state_key(project_root)
        / "runs"
        / run_id
        / "sdk_stream_events.jsonl",
        _control_claude_dir() / "runtime_state" / "session_observer" / "sdk_stream_events.jsonl",
    ]


def _runtime_run_root(project_root: Path, execution_input: dict[str, Any]) -> Path:
    run_id = _safe_path_component(str(execution_input.get("run_id") or "run"))
    return (
        _control_claude_dir()
        / "runtime_state"
        / "projects"
        / _project_state_key(project_root)
        / "runs"
        / run_id
    )


def _observer_progress_paths(project_root: Path, execution_input: dict[str, Any]) -> list[tuple[str, Path]]:
    run_root = _runtime_run_root(project_root, execution_input)
    return [
        ("event_log", run_root / "event_log.jsonl"),
        ("run_ledger", run_root / "run_ledger.json"),
        ("tool_events", run_root / "tool_events.jsonl"),
        ("agent_messages", run_root / "agent_messages.jsonl"),
        ("teammate_reports", run_root / "teammate_reports.jsonl"),
        ("completion_checks", run_root / "completion_checks.jsonl"),
        ("error_events", run_root / "error_events.jsonl"),
        ("session_events", run_root / "session_events.jsonl"),
        ("session_bindings", run_root / "session_bindings.jsonl"),
        ("bridge_packets", run_root / "bridge_packets.jsonl"),
        ("process_events", run_root / "process_events.jsonl"),
    ]


def _latest_observer_progress(project_root: Path, execution_input: dict[str, Any]) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    for stream_name, path in _observer_progress_paths(project_root, execution_input):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        if stat.st_size <= 0:
            continue
        if latest is None or float(stat.st_mtime) > float(latest.get("mtime_epoch") or 0.0):
            latest = {
                "stream_name": stream_name,
                "path": str(path),
                "mtime_epoch": float(stat.st_mtime),
                "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "size_bytes": int(stat.st_size),
            }
    return latest


def _observer_progress_epoch(progress: dict[str, Any] | None) -> float | None:
    if not isinstance(progress, dict):
        return None
    try:
        return float(progress.get("mtime_epoch"))
    except Exception:
        return None


def _reconcile_observed_teammate_activity(result: dict[str, Any], project_root: Path, execution_input: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict) or not _claims_missing_teammate_transport(result):
        return result
    observed = _observed_teammate_activity(project_root, execution_input)
    if not observed.get("teammates"):
        return result

    reconciled = dict(result)
    evidence = reconciled.get("evidence") if isinstance(reconciled.get("evidence"), dict) else {}
    evidence = dict(evidence)
    evidence["observer_reconciliation"] = observed
    evidence["observer_reconciliation_note"] = (
        "Diagnostic only: observer streams prove child activity was observed, not that the "
        "BridgeResult status, error classification, or task completion contract should change."
    )
    reconciled["evidence"] = evidence

    reports = reconciled.get("reports")
    if isinstance(reports, list):
        reconciled["reports"] = [_annotate_missing_teammate_report(report, observed) for report in reports]
    return reconciled


def _claims_missing_teammate_transport(result: dict[str, Any]) -> bool:
    if result.get("status") not in {"failed", "partial", "partial_or_failed"}:
        return False
    text = _compact_lower_text(result)
    missing_markers = (
        "missingteammatereport",
        "missing teammate",
        "missing implementor report",
        "no usable implementor report",
        "no usable teammate report",
        "no implementation evidence",
        "teammate_report_missing",
    )
    transport_markers = (
        "econnreset",
        "api error",
        "unable to connect to api",
        "connection reset",
        "socket hang up",
        "transport",
        "no report",
    )
    return any(marker in text for marker in missing_markers) and any(marker in text for marker in transport_markers)


def _observed_teammate_activity(project_root: Path, execution_input: dict[str, Any]) -> dict[str, Any]:
    run_root = _runtime_run_root(project_root, execution_input)
    filters = {
        "bridge_window_id": str(execution_input.get("bridge_window_id") or ""),
        "team_id": str(execution_input.get("team_id") or ""),
        "task_id": str(execution_input.get("task_id") or ""),
    }
    teammates: dict[str, dict[str, Any]] = {}
    latest_timestamp: str | None = None

    def teammate_bucket(name: str) -> dict[str, Any]:
        bucket = teammates.setdefault(
            name,
            {
                "teammate_id": name,
                "completed_agent_calls": 0,
                "completed_tool_calls": 0,
                "failed_tool_calls": 0,
                "session_started": 0,
                "tool_names": [],
                "agent_ids": [],
                "session_ids": [],
                "refs": [],
            },
        )
        return bucket

    for line_number, record in _read_jsonl_records(run_root / "tool_events.jsonl"):
        if not _record_matches_bridge_scope(record, filters):
            continue
        teammate = _teammate_name_from_record(record)
        if not teammate:
            continue
        bucket = teammate_bucket(teammate)
        status = str(record.get("status") or "").lower()
        tool_name = str(record.get("tool_name") or "")
        if status == "completed" and tool_name == "Agent":
            bucket["completed_agent_calls"] += 1
        elif status == "completed":
            bucket["completed_tool_calls"] += 1
        elif status == "failed":
            bucket["failed_tool_calls"] += 1
        if tool_name and tool_name not in bucket["tool_names"]:
            bucket["tool_names"].append(tool_name)
        _append_unique(bucket["refs"], f"tool_events.jsonl:{line_number}", limit=12)
        _append_unique(bucket["session_ids"], record.get("session_id"), limit=6)
        _append_unique(bucket["agent_ids"], record.get("agent_id"), limit=8)
        latest_timestamp = _max_timestamp(latest_timestamp, record.get("timestamp"))

    for line_number, record in _read_jsonl_records(run_root / "session_events.jsonl"):
        if not _record_matches_bridge_scope(record, filters):
            continue
        teammate = _teammate_name_from_record(record)
        if not teammate:
            continue
        event_type = str(record.get("event_type") or "")
        if event_type not in {"session_started", "tool_call_completed", "tool_call_failed"}:
            continue
        bucket = teammate_bucket(teammate)
        if event_type == "session_started":
            bucket["session_started"] += 1
        elif event_type == "tool_call_completed":
            bucket["completed_tool_calls"] += 1
        elif event_type == "tool_call_failed":
            bucket["failed_tool_calls"] += 1
        _append_unique(bucket["refs"], f"session_events.jsonl:{line_number}", limit=12)
        _append_unique(bucket["session_ids"], record.get("session_id"), limit=6)
        _append_unique(bucket["agent_ids"], record.get("agent_id"), limit=8)
        latest_timestamp = _max_timestamp(latest_timestamp, record.get("timestamp"))

    filtered = []
    for bucket in teammates.values():
        if bucket["completed_agent_calls"] or bucket["completed_tool_calls"] or bucket["session_started"]:
            bucket["tool_names"] = sorted(bucket["tool_names"])
            filtered.append(bucket)
    return {
        "classification": "teammate_report_collection_gap",
        "run_id": execution_input.get("run_id"),
        "bridge_window_id": execution_input.get("bridge_window_id"),
        "team_id": execution_input.get("team_id"),
        "task_id": execution_input.get("task_id"),
        "latest_observer_timestamp": latest_timestamp,
        "teammates": filtered,
        "note": (
            "Observer evidence is diagnostic only: it proves child activity happened, "
            "not that the teammate completed the task contract or that BridgeResult "
            "status/error classification should change."
        ),
    }


def _annotate_missing_teammate_report(report: Any, observed: dict[str, Any]) -> Any:
    if not isinstance(report, dict):
        return report
    annotated = dict(report)
    text = _compact_lower_text(report)
    if "no usable" not in text and "missing" not in text and "econnreset" not in text:
        return annotated
    note = (
        "Runtime observer saw completed teammate Agent/tool activity for this window; "
        "the remaining failure is report collection/parsing, not proven provider outage."
    )
    annotated["observer_reconciliation"] = observed
    annotated["diagnostic_note"] = note
    return annotated


def _read_jsonl_records(path: Path) -> list[tuple[int, dict[str, Any]]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError, UnicodeError):
        return []
    records: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append((line_number, record))
    return records


def _record_matches_bridge_scope(record: dict[str, Any], filters: dict[str, str]) -> bool:
    for key, expected in filters.items():
        if expected and str(record.get(key) or "") != expected:
            return False
    return True


def _teammate_name_from_record(record: dict[str, Any]) -> str | None:
    for key in ("teammate_id", "agent_type", "agent_id", "display_name"):
        value = str(record.get(key) or "").strip()
        if value in TEAMMATE_AGENT_NAMES:
            return value
    return None


def _append_unique(values: list[Any], value: Any, *, limit: int) -> None:
    if value is None:
        return
    text = str(value)
    if not text or text in values:
        return
    if len(values) < limit:
        values.append(text)


def _max_timestamp(current: str | None, candidate: Any) -> str | None:
    if not isinstance(candidate, str) or not candidate:
        return current
    if current is None or candidate > current:
        return candidate
    return current


def _compact_lower_text(value: Any, *, _depth: int = 0) -> str:
    if _depth > 5:
        return ""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, (int, float, bool)):
        return str(value).lower()
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            parts.append(str(key).lower())
            parts.append(_compact_lower_text(item, _depth=_depth + 1))
            if sum(len(part) for part in parts) > 30000:
                break
        return " ".join(part for part in parts if part)
    if isinstance(value, list):
        return " ".join(_compact_lower_text(item, _depth=_depth + 1) for item in value[:50])
    return str(value).lower()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sdk_payload_keys(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return []
    return sorted(str(key) for key in payload.keys())[:20]


def _sdk_stream_event_type(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return "sdk_stream_delta"
    if payload.get("type") == "content_block_delta":
        return "sdk_stream_content_block_delta"
    if _collect_tool_use_blocks(payload, limit=1):
        return "sdk_stream_tool_use"
    if _collect_tool_result_blocks(payload, limit=1):
        return "sdk_stream_tool_result"
    if _payload_has_assistant_text(payload):
        return "sdk_stream_assistant_text"
    return "sdk_stream_delta"


def _sdk_message_preview(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    if isinstance(payload, dict):
        text_delta = _sdk_text_delta(payload)
        if text_delta:
            parts.append(text_delta)
        for key in ("text", "message", "summary", "stop_reason", "subtype", "type"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        result = payload.get("result")
        if isinstance(result, str) and result.strip():
            parts.append(result.strip())
        content = payload.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
    elif payload is not None:
        parts.append(str(payload))

    preview = "\n".join(parts)
    if not preview and isinstance(payload, dict):
        tool_fields = _sdk_compact_tool_fields(payload)
        if tool_fields:
            preview = json.dumps(tool_fields, ensure_ascii=False, separators=(",", ":"))
    return _redact_sdk_text(preview)[:_SDK_STREAM_PREVIEW_LIMIT]


def _sdk_text_delta(payload: dict[str, Any]) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("text_delta", "delta_text"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return _redact_sdk_text(value)[:_SDK_STREAM_PREVIEW_LIMIT]
    delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
    if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
        return _redact_sdk_text(delta["text"])[:_SDK_STREAM_PREVIEW_LIMIT]
    if isinstance(delta.get("text"), str):
        return _redact_sdk_text(delta["text"])[:_SDK_STREAM_PREVIEW_LIMIT]
    return None


def _sdk_input_json_delta(payload: dict[str, Any]) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("input_json_delta", "partial_json"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return _redact_sdk_text(value)[:_SDK_STREAM_PREVIEW_LIMIT]
    delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
    if delta.get("type") == "input_json_delta" and isinstance(delta.get("partial_json"), str):
        return _redact_sdk_text(delta["partial_json"])[:_SDK_STREAM_PREVIEW_LIMIT]
    if isinstance(delta.get("partial_json"), str):
        return _redact_sdk_text(delta["partial_json"])[:_SDK_STREAM_PREVIEW_LIMIT]
    return None


def _sdk_compact_tool_fields(payload: dict[str, Any]) -> dict[str, Any]:
    tool_blocks = _collect_tool_use_blocks(payload, limit=1) or _collect_tool_result_blocks(payload, limit=1)
    if not tool_blocks and isinstance(payload, dict) and isinstance(payload.get("content_block"), dict):
        tool_blocks = [_compact_tool_use_block(payload["content_block"])]
    if not tool_blocks:
        return {}
    block = tool_blocks[0]
    compact: dict[str, Any] = {}
    for source_key, target_key in (
        ("id", "tool_id"),
        ("name", "tool_name"),
        ("tool_name", "tool_name"),
        ("server_name", "server_name"),
        ("type", "tool_block_type"),
    ):
        value = block.get(source_key)
        if isinstance(value, str) and value.strip() and target_key not in compact:
            compact[target_key] = value.strip()
    input_keys = block.get("input_keys")
    if isinstance(input_keys, list):
        compact["tool_input_keys"] = input_keys[:20]
    return compact


def _collect_tool_result_blocks(value: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if len(blocks) >= limit:
            return
        if isinstance(node, dict):
            if node.get("type") in {"tool_result", "server_tool_result"}:
                blocks.append(_compact_tool_result_block(node))
            for nested in node.values():
                if isinstance(nested, (dict, list)):
                    walk(nested)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return blocks


def _compact_tool_result_block(block: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {"type": block.get("type")}
    for key in ("id", "tool_use_id", "name", "tool_name", "server_name"):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            compact[key] = value
    return compact


def _payload_has_assistant_text(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("text"), str) and payload.get("text", "").strip():
        return True
    content = payload.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip() for item in content)


def _redact_sdk_text(text: str) -> str:
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)(\s*[:=]\s*)(\S+)", r"\1\2<redacted>", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-<redacted>", text)
    return text


def _attach_cli_debug_evidence(
    result: dict[str, Any],
    prompt_path: Path,
    stdout: str,
    stderr: str,
    *,
    payload: dict[str, Any] | None = None,
) -> None:
    evidence = result.setdefault("evidence", {})
    if not isinstance(evidence, dict):
        return
    debug_path = prompt_path.with_suffix(".cli_debug.json")
    debug_payload: dict[str, Any] = {
        "prompt_file": str(prompt_path),
        "stdout_tail": _redact_sdk_text(stdout)[-4000:],
        "stderr_tail": _redact_sdk_text(stderr)[-4000:],
        "truncated": len(stdout) > 4000 or len(stderr) > 4000,
    }
    if payload is not None:
        debug_payload["payload"] = payload
    debug_path.write_text(json.dumps(debug_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    evidence["cli_debug_file"] = str(debug_path)


def _bridge_prompt_path(project_root: Path, execution_input: dict[str, Any]) -> Path:
    raw_key = "|".join(
        str(execution_input.get(key) or "")
        for key in ("run_id", "sub_session_id", "bridge_window_id", "team_id", "task_id")
    )
    digest = hashlib.sha1(raw_key.encode("utf-8", errors="replace")).hexdigest()[:16]
    run_id = _safe_path_component(str(execution_input.get("run_id") or "run"))
    return (
        _control_claude_dir()
        / "runtime_state"
        / "projects"
        / _project_state_key(project_root)
        / "bridge_prompts"
        / run_id
        / f"{digest}.md"
    )


def _project_state_key(project_root: Path) -> str:
    normalized = str(project_root.resolve()).lower()
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{project_root.name}_{digest}"


def _safe_path_component(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value)
    return (cleaned[:48].strip("._") or "run")


def _repair_mojibake_value(value: Any) -> Any:
    if isinstance(value, str):
        return _repair_mojibake_text(value)
    if isinstance(value, list):
        return [_repair_mojibake_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _repair_mojibake_value(item) for key, item in value.items()}
    return value


def _repair_mojibake_text(value: str) -> str:
    if not value or not _looks_like_mojibake(value):
        return value
    best = value
    best_score = _text_quality_score(value)
    for encoding in ("gbk", "cp936"):
        for errors in ("strict", "ignore", "replace"):
            try:
                repaired = value.encode(encoding, errors=errors).decode("utf-8", errors="replace")
            except UnicodeError:
                continue
            score = _text_quality_score(repaired)
            if score > best_score:
                best = repaired
                best_score = score
    return best


def _looks_like_mojibake(value: str) -> bool:
    return any(marker in value for marker in _MOJIBAKE_MARKERS)


def _text_quality_score(value: str) -> int:
    score = sum(1 for char in value if "\u4e00" <= char <= "\u9fff")
    score += sum(value.count(term) for term in _EXPECTED_CJK_TERMS) * 20
    score -= sum(value.count(marker) for marker in _MOJIBAKE_MARKERS) * 12
    score -= value.count("\ufffd") * 20
    score -= value.count("?") * 2
    return score


_MOJIBAKE_MARKERS = (
    "\u951b",
    "\u6d93",
    "\u7eef",
    "\u5a34",
    "\u93c5",
    "\u95bf",
    "\u20ac",
    "\ufffd",
    "\ue75f",
    "\ue50b",
)

_EXPECTED_CJK_TERMS = (
    "\u7cfb\u7edf",
    "\u6d4b\u8bd5",
    "\u5f53\u524d",
    "\u9879\u76ee",
    "\u642d\u5efa",
    "\u6846\u67b6",
    "\u6267\u884c",
    "\u62a5\u9519",
    "\u5931\u8d25",
    "\u8bad\u7ec3",
    "\u68c0\u67e5",
)


def _control_claude_dir() -> Path:
    # __file__ = .claude/control/runtime/claude_cli_executor.py
    return Path(__file__).resolve().parents[2]


def _source_agent_dir() -> Path:
    return _control_claude_dir() / "agents"


def _settings_args(project_root: Path | None = None) -> list[str]:
    explicit = os.environ.get("BRIDGE_CLAUDE_SETTINGS")
    if explicit:
        return ["--settings", str(Path(explicit).expanduser().resolve())]

    parent_claude = _discover_parent_claude_dir(project_root)
    default_settings = parent_claude / "settings.json"
    hook_settings = parent_claude / "hooks" / "settings.json"
    if default_settings.exists() or hook_settings.exists():
        source = default_settings if default_settings.exists() else hook_settings
        return ["--settings", str(_materialize_bridge_settings(source, hook_settings=hook_settings, claude_root=parent_claude))]

    return []


def _read_settings_payload_strict(source: Path) -> dict[str, Any]:
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid Claude settings payload: {source}")
    return payload


_CLAUDE_CLI_HOOK_EVENTS = {
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PostToolBatch",
    "Notification",
    "UserPromptSubmit",
    "UserPromptExpansion",
    "SessionStart",
    "SessionEnd",
    "Stop",
    "StopFailure",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "PermissionRequest",
    "PermissionDenied",
    "Setup",
    "TeammateIdle",
    "TaskCreated",
    "TaskCompleted",
    "Elicitation",
    "ElicitationResult",
    "ConfigChange",
    "WorktreeCreate",
    "WorktreeRemove",
    "InstructionsLoaded",
    "CwdChanged",
    "FileChanged",
}


def _filter_claude_cli_hooks(hooks: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in hooks.items() if key in _CLAUDE_CLI_HOOK_EVENTS}


def _merge_authoritative_hooks(base: dict[str, Any], hook_settings: Path | None) -> dict[str, Any]:
    if not hook_settings or not hook_settings.exists():
        return dict(base)
    hook_payload = _read_settings_payload_strict(hook_settings)
    hooks = hook_payload.get("hooks")
    if not isinstance(hooks, dict):
        return dict(base)
    merged = dict(base)
    merged["hooks"] = _filter_claude_cli_hooks(hooks)
    return merged


def _settings_source_for_diagnostics(settings_path: Path | None) -> str | None:
    if not settings_path:
        return None
    claude_root = settings_path.parent.parent.parent if settings_path.name == "bridge_hooks_settings.json" and settings_path.parent.name == "generated" else settings_path.parent
    default_settings = claude_root / "settings.json"
    hook_settings = claude_root / "hooks" / "settings.json"
    if default_settings.exists() and hook_settings.exists():
        return f"{default_settings.resolve()} + {hook_settings.resolve()}"
    if hook_settings.exists():
        return str(hook_settings.resolve())
    if default_settings.exists():
        return str(default_settings.resolve())
    return str(settings_path)


def _settings_diagnostics(cmd: list[str], env: dict[str, str], project_root: Path | None = None) -> dict[str, Any]:
    settings_path = _settings_path_from_cmd(cmd)
    settings_payload = _read_settings_payload(settings_path)
    settings_env = settings_payload.get("env") if isinstance(settings_payload, dict) else None
    settings_env = settings_env if isinstance(settings_env, dict) else {}
    return {
        "claude_command": _claude_command_preview(cmd),
        "bridge_claude_command_configured": _has_nonempty(os.environ.get("BRIDGE_CLAUDE_COMMAND")),
        "bridge_claude_cli_configured": _has_nonempty(os.environ.get("BRIDGE_CLAUDE_CLI")),
        "settings_path": str(settings_path) if settings_path else None,
        "settings_path_exists": bool(settings_path and settings_path.exists()),
        "inferred_source_path": _infer_settings_source(settings_path),
        "bridge_claude_settings_env": _redacted_env_path(os.environ.get("BRIDGE_CLAUDE_SETTINGS")),
        "control_settings_exists": (_control_claude_dir() / "settings.json").exists(),
        "hook_settings_exists": (_control_claude_dir() / "hooks" / "settings.json").exists(),
        "settings_env_keys": sorted(str(key) for key in settings_env.keys()),
        "settings_has_anthropic_base_url": _has_nonempty(settings_env.get("ANTHROPIC_BASE_URL")),
        "settings_anthropic_base_url": _safe_url_preview(settings_env.get("ANTHROPIC_BASE_URL")),
        "settings_has_anthropic_auth_token": _has_nonempty(settings_env.get("ANTHROPIC_AUTH_TOKEN")),
        "settings_has_http_proxy": _has_nonempty(_env_value_ci(settings_env, "HTTP_PROXY")),
        "settings_has_https_proxy": _has_nonempty(_env_value_ci(settings_env, "HTTPS_PROXY")),
        "effective_anthropic_base_url": _safe_url_preview(_effective_anthropic_base_url(project_root=project_root, settings_path=settings_path)),
        "claude_print_bare_mode": "--bare" in cmd,
        "subprocess_env_has_anthropic_base_url": _has_nonempty(env.get("ANTHROPIC_BASE_URL")),
        "subprocess_anthropic_base_url": _safe_url_preview(env.get("ANTHROPIC_BASE_URL")),
        "subprocess_env_has_anthropic_auth_token": _has_nonempty(env.get("ANTHROPIC_AUTH_TOKEN")),
        "subprocess_env_has_anthropic_api_key": _has_nonempty(env.get("ANTHROPIC_API_KEY")),
        "subprocess_env_auth_token_aliased_to_api_key": _has_nonempty(env.get("ANTHROPIC_AUTH_TOKEN"))
        and env.get("ANTHROPIC_API_KEY") == env.get("ANTHROPIC_AUTH_TOKEN"),
        "subprocess_claude_code_simple": env.get("CLAUDE_CODE_SIMPLE"),
        "subprocess_env_has_http_proxy": _has_nonempty(_env_value_ci(env, "HTTP_PROXY")),
        "subprocess_env_has_https_proxy": _has_nonempty(_env_value_ci(env, "HTTPS_PROXY")),
    }


def _claude_command_preview(cmd: list[str]) -> str | None:
    if not cmd:
        return None
    stop = len(cmd)
    for marker in ("--settings", "-p", "--agent", "--model"):
        try:
            stop = min(stop, cmd.index(marker))
        except ValueError:
            continue
    prefix = cmd[: max(1, stop)]
    return " ".join(_redact_cmd(prefix))


def _merge_settings_env_into_subprocess_env(cmd: list[str], env: dict[str, str]) -> list[str]:
    settings_payload = _read_settings_payload(_settings_path_from_cmd(cmd))
    settings_env = settings_payload.get("env") if isinstance(settings_payload, dict) else None
    if not isinstance(settings_env, dict):
        return []
    copied: list[str] = []
    for key, value in settings_env.items():
        if value is None:
            continue
        env[str(key)] = str(value)
        copied.append(str(key))
    return sorted(copied)


def _merge_claude_command_env_into_subprocess_env(env: dict[str, str], project_root: Path | None = None) -> list[str]:
    command_env, _parts = _configured_claude_command(project_root)
    for key, value in command_env.items():
        env[key] = value
    return sorted(command_env.keys())


def _ensure_claude_api_key_alias(env: dict[str, str]) -> bool:
    """Claude CLI bare print mode requires ANTHROPIC_API_KEY."""
    if _has_nonempty(env.get("ANTHROPIC_API_KEY")):
        return False
    auth_token = env.get("ANTHROPIC_AUTH_TOKEN")
    if not _has_nonempty(auth_token):
        return False
    env["ANTHROPIC_API_KEY"] = str(auth_token)
    return True


def _should_use_bare_print_mode(project_root: Path | None = None) -> bool:
    override = _env_bool_override("BRIDGE_CLAUDE_PRINT_BARE")
    if override is not None:
        return override
    return False


def _env_bool_override(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _effective_anthropic_base_url(
    *,
    project_root: Path | None = None,
    settings_path: Path | None = None,
) -> str | None:
    for value in (
        os.environ.get("ANTHROPIC_BASE_URL"),
        os.environ.get("CLAUDE_CODE_API_BASE_URL"),
    ):
        if _has_nonempty(value):
            return str(value)

    candidate_paths: list[Path] = []
    if settings_path is not None:
        candidate_paths.append(settings_path)
    explicit_settings = os.environ.get("BRIDGE_CLAUDE_SETTINGS")
    if explicit_settings:
        candidate_paths.append(Path(explicit_settings).expanduser().resolve())

    parent_claude = _discover_parent_claude_dir(project_root)
    candidate_paths.extend(
        [
            parent_claude / "settings.json",
            parent_claude / "hooks" / "settings.json",
        ]
    )

    seen: set[Path] = set()
    for path in candidate_paths:
        try:
            resolved = path.expanduser().resolve()
        except Exception:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        payload = _read_settings_payload(resolved)
        settings_env = payload.get("env") if isinstance(payload, dict) else None
        if not isinstance(settings_env, dict):
            continue
        for key in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_API_BASE_URL"):
            value = settings_env.get(key)
            if _has_nonempty(value):
                return str(value)
    return None


def _is_custom_anthropic_base_url(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        parts = urlsplit(raw)
    except Exception:
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    return host not in {"api.anthropic.com", "claude.ai", "console.anthropic.com"}


def _settings_path_from_cmd(cmd: list[str]) -> Path | None:
    try:
        index = cmd.index("--settings")
    except ValueError:
        return None
    if index + 1 >= len(cmd):
        return None
    value = str(cmd[index + 1]).strip()
    return Path(value).expanduser().resolve() if value else None


def _read_settings_payload(settings_path: Path | None) -> dict[str, Any]:
    if not settings_path or not settings_path.exists():
        return {}
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _infer_settings_source(settings_path: Path | None) -> str | None:
    if not settings_path:
        return None
    if settings_path.name != "bridge_hooks_settings.json" or settings_path.parent.name != "generated":
        return str(settings_path)
    return _settings_source_for_diagnostics(settings_path)


def _redacted_env_path(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(Path(value).expanduser().resolve())
    except Exception:
        return "<set>"


def _has_nonempty(value: Any) -> bool:
    return bool(str(value or "").strip())


def _env_value_ci(env: dict[Any, Any], key: str) -> Any:
    target = key.lower()
    for item_key, value in env.items():
        if str(item_key).lower() == target:
            return value
    return None


def _safe_url_preview(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
    except Exception:
        return "<set>"
    if not parts.scheme or not parts.hostname:
        return "<set>"
    host = parts.hostname
    port = ""
    try:
        if parts.port is not None:
            port = f":{parts.port}"
    except ValueError:
        port = ""
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{host}{port}{path}"


def _materialize_bridge_settings(source: Path, *, hook_settings: Path | None = None, claude_root: Path | None = None) -> Path:
    source = source.expanduser().resolve()
    claude_root = (claude_root.expanduser().resolve() if claude_root else (source.parent.parent if source.parent.name == "hooks" else source.parent))
    payload = _merge_authoritative_hooks(_read_settings_payload_strict(source), hook_settings)
    normalized = _normalize_hook_commands(payload, claude_root)
    target = claude_root / "runtime_state" / "generated" / "bridge_hooks_settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def _normalize_hook_commands(value: Any, claude_root: Path | None = None) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_hook_commands(item, claude_root) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_hook_commands(item, claude_root) for item in value]
    if isinstance(value, str):
        return _absolute_hook_command(value, claude_root)
    return value


def _absolute_hook_command(command: str, claude_root: Path | None = None) -> str:
    hooks_root = (claude_root or _control_claude_dir()) / "hooks"
    normalized = command.replace("\\", "/")
    match = None
    for pattern in ("../.claude/hooks/", ".claude/hooks/"):
        index = normalized.find(pattern)
        if index >= 0:
            tail = normalized[index + len(pattern):].strip()
            script = tail.split()[0] if tail else ""
            if script:
                match = hooks_root / script
                break
    if match is None:
        return command
    return f"{_quote_cmd_arg(sys.executable)} {_quote_cmd_arg(str(match))}"


def _quote_cmd_arg(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def _ensure_project_agent_files(project_root: Path, names: list[str]) -> dict[str, Any]:
    source_dir = _source_agent_dir()
    missing: list[str] = []

    try:
        for name in names:
            source = source_dir / f"{name}.md"
            if not source.exists():
                missing.append(name)
    except Exception as exc:
        return {
            "source_dir": str(source_dir),
            "project_root": str(project_root),
            "copied": [],
            "missing": missing,
            "error_or_null": {
                "type": type(exc).__name__,
                "message": repr(exc),
            },
        }

    if missing:
        return {
            "source_dir": str(source_dir),
            "project_root": str(project_root),
            "copied": [],
            "missing": missing,
            "error_or_null": {
                "type": "MissingAgentFiles",
                "message": f"missing required agent files: {', '.join(missing)}",
            },
        }

    return {
        "source_dir": str(source_dir),
        "project_root": str(project_root),
        "copied": [],
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
    raw = os.environ.get("BRIDGE_ALLOWED_MODELS", "gpt-main,sonnet-main,deepseek-main")
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
    project_root: Path,
) -> dict[str, str]:
    env = os.environ.copy()
    env["BRIDGE_PROJECT_ROOT"] = str(project_root)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    if os.name != "nt":
        env.setdefault("LC_ALL", "C.UTF-8")
        env.setdefault("LANG", "C.UTF-8")

    _force_bridge_model_env(env, bridge_model)

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


def _force_bridge_model_env(env: dict[str, str], bridge_model: str) -> None:
    # Settings own provider connection details; agent frontmatter owns model routing.
    env["ANTHROPIC_MODEL"] = bridge_model
    env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = bridge_model
    env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = bridge_model
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = bridge_model


def _bind_bridge_child_session_env(env: dict[str, str], execution_input: dict[str, Any]) -> None:
    env["BRIDGE_CHILD_CLAUDE_SESSION"] = "1"
    run_id = str(execution_input.get("run_id") or "").strip()
    main_session_id = str(execution_input.get("main_session_id") or "").strip()
    sub_session_id = str(execution_input.get("sub_session_id") or "").strip()
    bridge_window_id = str(execution_input.get("bridge_window_id") or "").strip()
    team_id = str(execution_input.get("team_id") or "").strip()
    task_id = str(execution_input.get("task_id") or "").strip()

    if run_id:
        env["BRIDGE_RUN_ID"] = run_id
        env["CLAUDE_CONTROL_RUN_ID"] = run_id
    if main_session_id:
        env["BRIDGE_MAIN_SESSION_ID"] = main_session_id
        env["CLAUDE_CONTROL_MAIN_SESSION_ID"] = main_session_id
    if sub_session_id:
        env["BRIDGE_SUB_SESSION_ID"] = sub_session_id
    if bridge_window_id:
        env["BRIDGE_WINDOW_ID"] = bridge_window_id
    if team_id:
        env["BRIDGE_TEAM_ID"] = team_id
    if task_id:
        env["BRIDGE_TASK_ID"] = task_id


def _timeout_seconds(packet: dict[str, Any]) -> int:
    timeout_policy = packet.get("completion_contract", {}).get("timeout_policy") or {}
    hard_timeout = timeout_policy.get("hard_timeout_seconds")
    try:
        return max(30, int(hard_timeout))
    except Exception:
        return 3600


def _executor_timeout_seconds(packet: dict[str, Any]) -> int | None:
    if _executor_hard_timeout_disabled(packet):
        return None
    return _timeout_seconds(packet)


def _executor_hard_timeout_disabled(packet: dict[str, Any]) -> bool:
    if not isinstance(packet, dict):
        return False
    timeout_policy = packet.get("completion_contract", {}).get("timeout_policy") or {}
    if not isinstance(timeout_policy, dict):
        timeout_policy = {}
    if timeout_policy.get("executor_hard_timeout_disabled") is True:
        return True
    if str(packet.get("target_phase") or "") == "l4_execute" and timeout_policy.get("wait_until_process_complete") is True:
        return True
    return False


def _tmux_no_progress_watchdog_enabled(packet: dict[str, Any]) -> bool:
    return not _executor_hard_timeout_disabled(packet)


def _soft_timeout_seconds(packet: dict[str, Any], *, hard_timeout_seconds: int | None) -> int | None:
    if hard_timeout_seconds is None:
        return None
    timeout_policy = packet.get("completion_contract", {}).get("timeout_policy") if isinstance(packet, dict) else {}
    if not isinstance(timeout_policy, dict):
        return int(hard_timeout_seconds)
    soft_timeout = timeout_policy.get("soft_timeout_seconds")
    try:
        parsed = max(30, int(soft_timeout))
    except Exception:
        return int(hard_timeout_seconds)
    return min(parsed, int(hard_timeout_seconds))


def _tmux_no_progress_grace_seconds() -> int:
    raw = os.environ.get("BRIDGE_TMUX_NO_PROGRESS_GRACE_SECONDS")
    try:
        return max(30, int(raw)) if raw is not None else 300
    except Exception:
        return 300


def _claude_command_prefix(project_root: Path | None = None) -> list[str]:
    configured_cli = os.environ.get("BRIDGE_CLAUDE_CLI")
    if configured_cli and configured_cli.strip():
        return _claude_prefix_for_executable(configured_cli.strip())

    configured_env, configured_parts = _configured_claude_command(project_root)
    if configured_parts:
        # Supports either:
        #   BRIDGE_CLAUDE_COMMAND=claude
        #   BRIDGE_CLAUDE_COMMAND="C:\path\to\claude.cmd"
        #   BRIDGE_CLAUDE_COMMAND="claude --some-wrapper-arg"
        #   BRIDGE_CLAUDE_COMMAND="HOME=/data03/liang/mjy claude --mcp-config /data03/liang/mjy/.claude/mcp.json"
        return configured_parts
    if configured_env:
        return ["claude"]

    if os.environ.get("BRIDGE_DISABLE_CLAUDE_MJY_AUTO", "").strip().lower() not in {"1", "true", "yes"}:
        preferred = shutil.which("claude_mjy")
        if preferred:
            return _claude_prefix_for_resolved(preferred)

    resolved = shutil.which("claude")
    if not resolved:
        return ["claude"]

    return _claude_prefix_for_resolved(resolved)


def _claude_tty_command_prefix(project_root: Path | None = None) -> list[str]:
    configured_cli = os.environ.get("BRIDGE_CLAUDE_CLI")
    if configured_cli and configured_cli.strip():
        return _claude_prefix_for_executable(configured_cli.strip())

    _configured_env, configured_parts = _configured_claude_command(project_root)
    if configured_parts:
        stripped = _strip_claude_mcp_args(configured_parts)
        return stripped or ["claude"]

    resolved = shutil.which("claude")
    return _claude_prefix_for_resolved(resolved) if resolved else ["claude"]


def _strip_claude_mcp_args(parts: list[str]) -> list[str]:
    stripped: list[str] = []
    index = 0
    while index < len(parts):
        item = parts[index]
        if item == "--mcp-config":
            index += 2
            continue
        if item.startswith("--mcp-config=") or item == "--strict-mcp-config":
            index += 1
            continue
        stripped.append(item)
        index += 1
    return stripped


def should_use_tmux_bridge_executor(project_root: Path | None = None) -> bool:
    override = _env_bool_override("BRIDGE_TMUX_EXECUTOR")
    if override is not None:
        return override
    if os.name == "nt" or not shutil.which("tmux"):
        return False
    return _is_custom_anthropic_base_url(_effective_anthropic_base_url(project_root=project_root))


def _claude_prefix_for_executable(value: str) -> list[str]:
    expanded = Path(value).expanduser()
    if expanded.is_absolute() or any(sep in value for sep in ("/", "\\")):
        return _claude_prefix_for_resolved(str(expanded))
    return _claude_prefix_for_resolved(shutil.which(value) or value)


def _configured_claude_command(project_root: Path | None = None) -> tuple[dict[str, str], list[str]]:
    configured = os.environ.get("BRIDGE_CLAUDE_COMMAND")
    if configured and configured.strip():
        try:
            parts = shlex.split(configured, posix=(os.name != "nt"))
        except ValueError:
            return {}, [configured]
    else:
        if os.environ.get("BRIDGE_CLAUDE_CLI") or os.environ.get("OUTER_LEADER_CLAUDE_CLI"):
            return {}, []
        if os.environ.get("BRIDGE_DISABLE_CLAUDE_STARTUP_DEFAULTS", "").strip().lower() in {"1", "true", "yes"}:
            return {}, []
        parts = _default_claude_command_parts(project_root)
        if not parts:
            return {}, []
    env: dict[str, str] = {}
    while parts and _shell_env_assignment(parts[0]):
        key, value = parts.pop(0).split("=", 1)
        env[key] = value
    return env, parts


def _default_claude_command_parts(project_root: Path | None = None) -> list[str]:
    if os.environ.get("BRIDGE_DISABLE_CLAUDE_STARTUP_DEFAULTS", "").strip().lower() in {"1", "true", "yes"}:
        return []
    claude_root = _discover_parent_claude_dir(project_root)
    mcp_config = claude_root / "mcp.json"
    if not mcp_config.exists():
        return []
    return [f"HOME={claude_root.parent}", "claude", "--mcp-config", str(mcp_config)]


def _discover_parent_claude_dir(project_root: Path | None = None) -> Path:
    if project_root is None:
        raw_project_root = os.environ.get("BRIDGE_PROJECT_ROOT")
        if raw_project_root:
            project_root = Path(raw_project_root)
    if project_root is not None:
        candidate = Path(project_root).expanduser().resolve().parent / ".claude"
        if candidate.exists():
            return candidate
    return _control_claude_dir()


def _shell_env_assignment(value: str) -> bool:
    key, sep, _rest = value.partition("=")
    if sep != "=" or not key:
        return False
    if not (key[0].isalpha() or key[0] == "_"):
        return False
    return all(ch.isalnum() or ch == "_" for ch in key)


def _claude_prefix_for_resolved(resolved: str) -> list[str]:
    path = Path(resolved)

    exe_from_npm = path.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    if path.stem.lower() == "claude" and exe_from_npm.exists():
        return [str(exe_from_npm)]

    suffix = path.suffix.lower()
    if suffix in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(path)]
    if suffix == ".ps1":
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)]

    return [str(path)]


def _claude_print_stream_json_args() -> list[str]:
    # Claude CLI requires --verbose when print mode uses stream-json output.
    # --include-partial-messages keeps content_block_delta records visible for
    # UI-safe SDK stream text/input deltas.
    return ["--output-format", "stream-json", "--verbose", "--include-partial-messages"]


def _tmux_bridge_session_name(execution_input: dict[str, Any]) -> str:
    raw = "bridge_{run}_{sub}_{task}_{suffix}".format(
        run=execution_input.get("run_id") or "run",
        sub=execution_input.get("sub_session_id") or "sub",
        task=execution_input.get("task_id") or "task",
        suffix=uuid.uuid4().hex[:8],
    )
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)[:120]


def _tmux_launch_command(cmd: list[str], project_root: Path, env: dict[str, str]) -> str:
    public_env = _tmux_public_env(env)
    env_parts = [f"{key}={shlex.quote(str(value))}" for key, value in sorted(public_env.items()) if str(value)]
    command_parts = [shlex.quote(str(part)) for part in cmd]
    return " ".join(["cd", shlex.quote(str(project_root)), "&&", "env", *env_parts, *command_parts])


def _tmux_public_env(env: dict[str, str]) -> dict[str, str]:
    keys = [
        "HOME",
        "BRIDGE_PROJECT_ROOT",
        "BRIDGE_CHILD_CLAUDE_SESSION",
        "BRIDGE_RUN_ID",
        "CLAUDE_CONTROL_RUN_ID",
        "BRIDGE_MAIN_SESSION_ID",
        "CLAUDE_CONTROL_MAIN_SESSION_ID",
        "BRIDGE_SUB_SESSION_ID",
        "BRIDGE_WINDOW_ID",
        "BRIDGE_TEAM_ID",
        "BRIDGE_TASK_ID",
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "PYTHONIOENCODING",
        "PYTHONNOUSERSITE",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUTF8",
        "LC_ALL",
        "LANG",
    ]
    return {key: env[key] for key in keys if _has_nonempty(env.get(key))}


def _wait_for_tmux_ready(session_name: str, *, timeout: int) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = _tmux_capture(session_name)
        if "Claude Code" in last and ("❯" in last or ">" in last or "? for shortcuts" in last):
            return
        time.sleep(0.5)
    raise TimeoutError(f"Claude TTY did not become ready within {timeout}s: {last[-1000:]}")


def _tmux_paste_prompt(session_name: str, prompt: str) -> None:
    buffer_name = f"bridge_prompt_{uuid.uuid4().hex[:8]}"
    _tmux_run(["tmux", "load-buffer", "-b", buffer_name, "-"], input_text=prompt)
    _tmux_run(["tmux", "paste-buffer", "-b", buffer_name, "-t", session_name])
    _tmux_run(["tmux", "delete-buffer", "-b", buffer_name], check=False)
    time.sleep(_tmux_submit_delay_seconds(prompt))
    _tmux_run(["tmux", "send-keys", "-t", session_name, "Enter"])


def _tmux_submit_delay_seconds(prompt: str) -> float:
    return min(2.0, max(0.2, len(prompt) / 20000.0))


def _tmux_capture(session_name: str) -> str:
    return _tmux_run(["tmux", "capture-pane", "-p", "-J", "-t", session_name, "-S", "-2000"]).stdout


def _tmux_terminal_error(capture: str) -> dict[str, str] | None:
    tail = capture[-8000:]
    lines = tail.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        message = _tmux_terminal_api_error_line(lines[index])
        if not message:
            continue
        after_error = "\n".join(lines[index + 1 :])
        if not _tmux_prompt_visible(after_error):
            continue
        return {"type": "ClaudeTmuxTerminalApiError", "message": message}
    return None


def _tmux_terminal_api_error_line(line: str) -> str | None:
    candidate = re.sub(r"\s+", " ", str(line or "")).strip()
    if not candidate:
        return None
    if candidate.startswith("⎿"):
        candidate = candidate.lstrip("⎿").strip()
    for prefix in ("API Error:", "Unable to connect to API"):
        if candidate.startswith(prefix):
            return candidate
    if candidate.startswith("Error:") and "500 Internal Server Error" in candidate:
        return candidate
    if candidate.startswith("500 Internal Server Error"):
        return candidate
    return None


def _tmux_prompt_visible(text: str) -> bool:
    return "? for shortcuts" in text and ("❯" in text or ">" in text)


def _tmux_run(
    args: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
        timeout=30,
    )


def _tmux_assistant_text(capture: str, prompt: str) -> str:
    try:
        from outer_sdk.tmux_repl_adapter import extract_assistant_text

        text = extract_assistant_text(capture, prompt)
    except Exception:
        text = ""
    return text or _assistant_text_after_prompt(capture, prompt)


def _assistant_text_after_prompt(capture: str, prompt: str) -> str:
    prefix = prompt[: min(len(prompt), 200)]
    index = capture.rfind(prefix)
    if index >= 0:
        tail = capture[index + len(prefix) :]
        json_marker = tail.rfind("● {")
        if json_marker >= 0:
            return tail[json_marker + 1 :]
        return tail
    return capture


def _command_too_long_for_windows(cmd: list[str]) -> int | None:
    if os.name != "nt":
        return None
    command_length = sum(len(part) + 3 for part in cmd)
    return command_length if command_length > 30000 else None


def _parse_claude_payload(stdout: str, stderr: str) -> dict[str, Any]:
    envelope = _parse_claude_stdout_envelope(stdout)
    if envelope is None:
        return _failure(
            message="claude cli bridge executor returned non-json output",
            error_type="ClaudeCliNonJsonOutput",
            evidence={
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
            },
        )

    payload = _extract_bridge_payload(envelope)
    if _is_tool_use_without_final_result(envelope, payload):
        return {
            "payload": _tool_use_incomplete_bridge_payload(envelope, stdout, stderr),
            "error_or_null": None,
        }
    if _is_empty_claude_result_envelope(envelope, payload):
        return _failure(
            message="claude cli bridge executor returned an empty structured result",
            error_type="ClaudeCliEmptyResult",
            evidence={
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
                "envelope": envelope,
            },
        )

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


def _parse_claude_stdout_envelope(stdout: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    parsed_lines: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        parsed_line = _parse_json_object_text(line)
        if parsed_line is not None:
            parsed_lines.append(parsed_line)
    if not parsed_lines:
        return None

    for item in reversed(parsed_lines):
        if item.get("type") == "result":
            return item
    for item in reversed(parsed_lines):
        if item.get("status") in {"succeeded", "failed", "partial", "partial_or_failed"}:
            return item
    return parsed_lines[-1]


def _is_tool_use_without_final_result(envelope: Any, extracted_payload: Any) -> bool:
    if not isinstance(envelope, dict):
        return False
    if _envelope_stop_reason(envelope) != "tool_use":
        return False
    if isinstance(extracted_payload, dict) and extracted_payload.get("status") in {"succeeded", "failed", "partial", "partial_or_failed"}:
        return False
    return _is_empty_claude_result_envelope(envelope, extracted_payload) or not _has_structured_bridge_payload(extracted_payload)


def _tool_use_incomplete_bridge_payload(envelope: dict[str, Any], stdout: str, stderr: str) -> dict[str, Any]:
    pending_tool_uses = _collect_tool_use_blocks(envelope)
    return {
        "status": "partial_or_failed",
        "reports": [
            {
                "summary": "Claude CLI stopped on tool_use before emitting a final bridge report.",
                "failure_reason": "The CLI process returned a successful envelope, but the final bridge result was empty or missing while stop_reason=tool_use.",
                "next_action_recommendation": "Inspect the CLI debug artifact and worktree state, then retry or resume the bridge after the pending tool-use turn is resolved.",
            }
        ],
        "artifact_refs": [],
        "evidence": {
            "classification": "incomplete_no_final_text",
            "stop_reason": "tool_use",
            "result_empty": _result_is_empty(envelope.get("result")),
            "pending_tool_uses": pending_tool_uses,
            "pending_tool_use_count": len(pending_tool_uses),
            "stdout_tail": stdout[-4000:],
            "stderr_tail": stderr[-4000:],
            "envelope": envelope,
        },
        "error_or_null": {
            "message": "claude cli stopped on tool_use before emitting a final bridge result",
            "type": "ClaudeCliNeedsToolContinuation",
            "stop_reason": "tool_use",
        },
        "cleanup_required": False,
    }


def _has_structured_bridge_payload(value: Any) -> bool:
    return isinstance(value, dict) and value.get("status") in {"succeeded", "failed", "partial", "partial_or_failed"}


def _is_empty_claude_result_envelope(envelope: Any, extracted_payload: Any) -> bool:
    if not isinstance(envelope, dict):
        return False
    if not ("result" in envelope or envelope.get("type") == "result" or "subtype" in envelope):
        return False
    result = envelope.get("result")
    if result is None:
        return True
    if isinstance(result, str) and not result.strip():
        return True
    if isinstance(result, (dict, list)) and not result:
        return True
    return isinstance(extracted_payload, dict) and not extracted_payload and isinstance(result, dict)


def _result_is_empty(result: Any) -> bool:
    if result is None:
        return True
    if isinstance(result, str):
        return not result.strip()
    if isinstance(result, (dict, list)):
        return not result
    return False


def _envelope_stop_reason(value: Any) -> str | None:
    if isinstance(value, dict):
        reason = value.get("stop_reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
        for key in ("message", "result", "response", "output", "payload"):
            found = _envelope_stop_reason(value.get(key))
            if found:
                return found
        content = value.get("content")
        if isinstance(content, list):
            found = _envelope_stop_reason(content)
            if found:
                return found
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                found = _envelope_stop_reason(nested)
                if found:
                    return found
    elif isinstance(value, list):
        for item in value:
            found = _envelope_stop_reason(item)
            if found:
                return found
    return None


def _collect_tool_use_blocks(value: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if len(blocks) >= limit:
            return
        if isinstance(node, dict):
            block_type = node.get("type")
            if block_type in {"tool_use", "server_tool_use"}:
                blocks.append(_compact_tool_use_block(node))
            for nested in node.values():
                if isinstance(nested, (dict, list)):
                    walk(nested)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return blocks


def _compact_tool_use_block(block: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "type": block.get("type"),
    }
    for key in ("id", "name", "tool_name", "server_name"):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            compact[key] = value
    tool_input = block.get("input")
    if isinstance(tool_input, dict):
        compact["input_keys"] = sorted(str(key) for key in tool_input.keys())
    elif tool_input is not None:
        compact["input_type"] = type(tool_input).__name__
    return compact


def _extract_bridge_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload

    if payload.get("status") in {"succeeded", "failed", "partial", "partial_or_failed"}:
        return payload

    structured = payload.get("structured_output")
    if isinstance(structured, dict):
        return _extract_bridge_payload(structured)
    if isinstance(structured, str):
        parsed = _parse_json_object_text(structured)
        if parsed:
            return _extract_bridge_payload(parsed)

    result = payload.get("result")
    if isinstance(result, dict):
        return _extract_bridge_payload(result)
    if isinstance(result, str):
        parsed = _parse_json_object_text(result)
        if parsed:
            return _extract_bridge_payload(parsed)

    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parsed = _parse_json_object_text(text)
                if parsed:
                    return _extract_bridge_payload(parsed)

    return payload


def _parse_json_object_text(text: str) -> dict[str, Any] | None:
    s = text.strip()
    if not s:
        return None
    if s.startswith("```"):
        lines = s.splitlines()
        if len(lines) >= 3:
            s = "\n".join(lines[1:-1]).strip()
    candidates = [s]
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end > start:
        candidates.append(s[start : end + 1])
    for candidate in candidates:
        for normalized in _json_text_variants(candidate):
            try:
                parsed = json.loads(normalized)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _parse_bridge_json_from_text(text: str) -> dict[str, Any] | None:
    parsed = _parse_json_object_text(text)
    if _has_structured_bridge_payload(parsed):
        return parsed

    s = text.strip()
    if not s:
        return None
    starts = [match.start() for match in re.finditer(r"\{", s)]
    ends = [match.start() + 1 for match in re.finditer(r"\}", s)]
    for start in reversed(starts):
        for end in reversed(ends):
            if end <= start:
                continue
            for normalized in _json_text_variants(s[start:end]):
                try:
                    candidate = json.loads(normalized)
                except json.JSONDecodeError:
                    continue
                if _has_structured_bridge_payload(candidate):
                    return candidate
    return None


def _json_text_variants(text: str) -> list[str]:
    variants = [text]
    flattened = text.replace("\r", " ").replace("\n", " ")
    if flattened != text:
        variants.append(flattened)
    return variants


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

    reports = payload.get("reports")
    if isinstance(reports, dict):
        payload["reports"] = [reports]

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

    validation = validate_bridge_result(payload)
    if not validation.get("valid"):
        return _failure(
            message="claude cli bridge executor returned structurally invalid bridge result",
            error_type=str(validation.get("error_type") or "BridgeResultGuardrailFailed"),
            evidence={
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
                "payload": payload,
                "guardrail_validation": validation,
            },
        )

    return payload


def _redact_cmd(cmd: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    redact_positional_prompt = False

    sensitive_flags = {
        "--api-key",
        "--auth-token",
        "--token",
        "--password",
    }
    has_print_mode = "-p" in cmd or "--print" in cmd

    for part in cmd:
        if redact_positional_prompt:
            redacted.append(f"<prompt:{len(part)} chars>")
            continue

        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue

        if part in sensitive_flags:
            redacted.append(part)
            redact_next = True
            continue

        if part == "--" and has_print_mode:
            redacted.append(part)
            redact_positional_prompt = True
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

        loaded[name] = agent_config

    return loaded


def _compact_teammate_prompt(name: str, role: str, responsibilities: list[Any]) -> str:
    responsibility_text = "; ".join(str(item) for item in responsibilities)
    agent_path = f".claude/agents/{name}.md"
    return (
        f"You are {name}, a {role} teammate for one bridge window. "
        f"Follow your static {agent_path} instruction; do not read the bridge prompt artifact for task context. "
        "The BridgePacket assignment, tool boundary, and ownership boundary override any broader default behavior. "
        "When using Read, omit optional parameters you do not need; never pass pages as an empty string. "
        "Return concise evidence and findings to bridge-leader. "
        f"Packet responsibilities: {responsibility_text}"
    )


def _parse_tools(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]
