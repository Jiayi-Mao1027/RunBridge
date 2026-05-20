from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from .adapters import OuterLeaderEventSink
from .claude_agent_adapter import (
    _build_user_prompt,
    _ensure_env_api_key_alias,
    _env_int,
    _leader_model,
    _leader_prompt,
    _outer_leader_allowed_tools,
    _outer_leader_cli_info,
    _outer_leader_disallowed_tools,
    _outer_leader_permission_mode,
    _outer_leader_settings_path,
    _outer_leader_tools,
    _settings_env,
)
from .tmux_repl_adapter import _outer_leader_add_dirs, _outer_leader_tmux_bare_mode


class ClaudePrintOuterLeaderAdapter:
    name = "claude-print"

    def __init__(self, config: Any) -> None:
        self.config = config
        self._sequence = 0

    def handle_user_input(
        self,
        request: dict[str, Any],
        *,
        event_sink: OuterLeaderEventSink | None = None,
    ) -> dict[str, Any]:
        control_root = Path(self.config.control_root)
        repo_root = Path(self.config.repo_root) if self.config.repo_root else Path.cwd()
        prompt = _build_user_prompt(request)
        cmd, env, diagnostics = self._command(control_root, repo_root, request)
        self._emit(
            event_sink,
            "sdk_stream_started",
            {"session_id": request.get("main_session_id"), "input_id": request.get("input_id"), "outer_leader_options": diagnostics},
            status="streaming",
        )
        timeout = _print_timeout_seconds()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(repo_root),
                env=env,
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self._emit(event_sink, "sdk_stream_timeout", {"timeout_seconds": timeout}, status="failed")
            return _blocked_result(
                "OuterLeaderPrintTimeout",
                f"Outer leader print fallback did not return within {timeout} seconds.",
                request,
                evidence={"stdout_tail": _tail(exc.stdout), "stderr_tail": _tail(exc.stderr), "outer_leader_options": diagnostics},
            )

        parsed = _parse_stream_json(proc.stdout)
        for event in parsed["events"]:
            self._emit(event_sink, event["event_type"], event["payload"], status=event.get("status") or "streaming")
        result = _result_from_process(request, proc, parsed, diagnostics)
        self._emit(
            event_sink,
            "sdk_stream_final_result",
            {
                "session_id": request.get("main_session_id"),
                "returncode": proc.returncode,
                "result": parsed.get("result_text") or parsed.get("assistant_text") or "",
                "tool_names": parsed.get("tool_names") or [],
            },
            status="completed" if result.get("status") == "succeeded" else "failed",
        )
        self._emit(event_sink, "sdk_stream_final", {"session_id": request.get("main_session_id"), "message_count": len(parsed["events"])}, status="completed")
        return result

    def _command(self, control_root: Path, repo_root: Path, request: dict[str, Any]) -> tuple[list[str], dict[str, str], dict[str, Any]]:
        model = _leader_model(control_root)
        cli_info = _outer_leader_cli_info(control_root, repo_root)
        settings_path = _outer_leader_settings_path(control_root, cli_info, repo_root)
        cmd = [str(cli_info.get("cli_path") or "claude")]
        if _outer_leader_tmux_bare_mode(settings_path):
            cmd.append("--bare")
        cmd.append("-p")
        if cli_info.get("mcp_config"):
            cmd.extend(["--mcp-config", str(cli_info["mcp_config"])])
        if cli_info.get("strict_mcp_config", True):
            cmd.append("--strict-mcp-config")
        if settings_path:
            cmd.extend(["--settings", str(settings_path)])
        cmd.extend(
            [
                "--agent",
                "leader-orchestrator",
                "--model",
                model,
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
                "--include-hook-events",
                "--append-system-prompt",
                _leader_prompt(control_root),
            ]
        )
        for directory in _outer_leader_add_dirs(control_root, repo_root):
            cmd.extend(["--add-dir", str(directory)])
        permission_mode = _outer_leader_permission_mode()
        if permission_mode:
            cmd.extend(["--permission-mode", permission_mode])
        tools = _outer_leader_tools()
        if tools:
            cmd.extend(["--tools", ",".join(tools)])
        allowed_tools = _outer_leader_allowed_tools(tools)
        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])
        disallowed_tools = _outer_leader_disallowed_tools()
        if disallowed_tools:
            cmd.extend(["--disallowedTools", ",".join(disallowed_tools)])

        env = os.environ.copy()
        env.update(
            {
                "CLAUDE_CONTROL_ROOT": str(control_root),
                "BRIDGE_RUNTIME_REPO_KEY": str(request.get("repo_key") or ""),
                "BRIDGE_RUN_ID": str(request.get("run_id") or ""),
                "BRIDGE_MAIN_SESSION_ID": str(request.get("main_session_id") or ""),
                "CLAUDE_CONTROL_RUN_ID": str(request.get("run_id") or ""),
                "CLAUDE_CONTROL_MAIN_SESSION_ID": str(request.get("main_session_id") or ""),
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        env.update(_settings_env(settings_path))
        env.update({str(key): str(value) for key, value in (cli_info.get("env") or {}).items()})
        env["ANTHROPIC_MODEL"] = model
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = model
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = model
        _ensure_env_api_key_alias(env)
        diagnostics = {
            "adapter": self.name,
            "cli_path": cmd[0],
            "cli_source": cli_info.get("cli_source"),
            "cli_mcp_config": cli_info.get("mcp_config"),
            "model": model,
            "tools": tools,
            "allowed_tools": allowed_tools,
            "disallowed_tools": disallowed_tools,
        }
        return cmd, env, diagnostics

    def _emit(self, event_sink: OuterLeaderEventSink | None, event_type: str, payload: dict[str, Any], *, status: str = "streaming") -> None:
        if event_sink is None:
            return
        self._sequence += 1
        event_sink(event_type, payload, status=status, sequence=self._sequence)


def _parse_stream_json(stdout: str) -> dict[str, Any]:
    assistant_parts: list[str] = []
    tool_names: list[str] = []
    events: list[dict[str, Any]] = []
    result_text = ""
    for line in str(stdout or "").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload_type = str(payload.get("type") or "")
        message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    text = str(block.get("text") or "")
                    assistant_parts.append(text)
                    events.append({"event_type": "sdk_stream_assistant_text", "payload": {"text_delta": text, "sdk_message_type": payload_type}, "status": "completed"})
                if block.get("type") == "tool_use":
                    tool_name = str(block.get("name") or "")
                    if tool_name:
                        tool_names.append(tool_name)
                    events.append(
                        {
                            "event_type": "sdk_stream_tool_use",
                            "payload": {
                                "tool_name": tool_name,
                                "tool_use_id": block.get("id"),
                                "tool_input_keys": sorted((block.get("input") or {}).keys()) if isinstance(block.get("input"), dict) else [],
                                "sdk_message_type": payload_type,
                            },
                            "status": "streaming",
                        }
                    )
        if payload_type == "result":
            result_text = str(payload.get("result") or "")
    return {
        "assistant_text": "\n".join(part for part in assistant_parts if part).strip(),
        "result_text": result_text.strip(),
        "tool_names": tool_names,
        "events": events,
    }


def _result_from_process(request: dict[str, Any], proc: subprocess.CompletedProcess[str], parsed: dict[str, Any], diagnostics: dict[str, Any]) -> dict[str, Any]:
    text = str(parsed.get("result_text") or parsed.get("assistant_text") or "").strip()
    tool_names = [str(item) for item in parsed.get("tool_names") or []]
    called_bridge = any(name in {"mcp__bridge__call_bridge_sdk", "call_bridge_sdk"} for name in tool_names)
    evidence = {
        "adapter": "claude-print",
        "returncode": proc.returncode,
        "tool_names": tool_names,
        "outer_leader_options": diagnostics,
        "stderr_tail": _tail(proc.stderr),
    }
    if proc.returncode != 0:
        return _blocked_result("OuterLeaderPrintCliFailed", _tail(proc.stderr) or f"Claude print exited with {proc.returncode}.", request, evidence=evidence)
    if called_bridge:
        summary = "outer leader print fallback called bridge SDK"
    else:
        summary = text
    if summary:
        return {
            "status": "succeeded",
            "handled_by": "claude-print",
            "reports": [{"summary": summary, "source": "outer_leader_print"}],
            "artifact_refs": [],
            "evidence": evidence,
            "error_or_null": None,
            "cleanup_required": False,
        }
    return _blocked_result("OuterLeaderPrintNoAssistantText", "Claude print returned without assistant text or bridge tool use.", request, evidence=evidence)


def _blocked_result(error_type: str, message: str, request: dict[str, Any], *, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": "blocked",
        "handled_by": "claude-print",
        "reports": [],
        "artifact_refs": [],
        "evidence": {
            "outer_sdk_migration_point": "outer_sdk.print_adapter.ClaudePrintOuterLeaderAdapter",
            "run_id": request.get("run_id"),
            "repo_key": request.get("repo_key"),
            "main_session_id": request.get("main_session_id"),
            **(evidence or {}),
        },
        "error_or_null": {"type": error_type, "message": message},
        "cleanup_required": False,
    }


def _print_timeout_seconds() -> int:
    raw = _env_int("OUTER_LEADER_PRINT_TIMEOUT_SECONDS")
    if raw is None:
        return 300
    return max(30, min(raw, 3600))


def _tail(value: Any, limit: int = 2000) -> str:
    if value is None:
        return ""
    text = str(value)
    return text[-limit:]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
