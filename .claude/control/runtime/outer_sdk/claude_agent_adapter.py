from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import importlib
import inspect
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import threading
from typing import Any
from urllib.parse import urlsplit
import uuid

from .adapters import OuterLeaderEventSink


SDK_PACKAGE = "claude_agent_sdk"
DEFAULT_ALLOWED_TOOLS = [
    "mcp__bridge__list_registered_repos",
    "mcp__bridge__list_runs",
    "mcp__bridge__read_runtime_snapshot",
    "mcp__bridge__build_bridge_packet",
    "mcp__bridge__call_bridge_sdk",
    "mcp__bridge__reconcile_workflow_from_ledger",
    "mcp__bridge__mark_bridge_orphaned",
    "Read",
    "Grep",
    "Glob",
    "LS",
]
OUTER_LEADER_FORBIDDEN_TOOLS = {"Agent"}
DEFAULT_DISALLOWED_TOOLS = ["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "Agent"]
DEFAULT_PERMISSION_MODE = "dontAsk"
PREVIEW_LIMIT = 700
REPORT_TEXT_LIMIT = 20000


class ClaudeAgentSdkOuterLeaderAdapter:
    """Long-lived outer leader adapter backed by Claude Agent SDK.

    The adapter owns one persistent ClaudeSDKClient per host process. Each
    user input is sent into that same client session, while SDK messages are
    normalized into UI-safe stream records by the host event sink.
    """

    name = "claude-agent-sdk"

    def __init__(self, config: Any) -> None:
        self.config = config
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._loop_ready = threading.Event()
        self._client: Any = None
        self._sdk: Any = None
        self._request_lock = threading.Lock()
        self._sequence = 0
        self._options_diagnostics: dict[str, Any] = {}

    def handle_user_input(
        self,
        request: dict[str, Any],
        *,
        event_sink: OuterLeaderEventSink | None = None,
    ) -> dict[str, Any]:
        with self._request_lock:
            loop = self._ensure_loop()
            timeout = _sdk_timeout_seconds()
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._handle_user_input_async(dict(request), event_sink),
                    loop,
                )
                if timeout is None:
                    return future.result()
                return future.result(timeout=timeout)
            except TimeoutError:
                self._emit(event_sink, request, "sdk_stream_timeout", {"timeout_seconds": timeout}, status="failed")
                return _blocked_result(
                    "OuterLeaderSdkTimeout",
                    f"Outer leader SDK session did not return within {timeout} seconds.",
                    request,
                    handled_by=self.name,
                )
            except Exception as exc:
                self._emit(event_sink, request, "sdk_stream_error", {"error_type": type(exc).__name__, "message": str(exc)}, status="failed")
                return _blocked_result(
                    "OuterLeaderSdkError",
                    str(exc),
                    request,
                    handled_by=self.name,
                )

    async def _handle_user_input_async(
        self,
        request: dict[str, Any],
        event_sink: OuterLeaderEventSink | None,
    ) -> dict[str, Any]:
        sdk = self._load_sdk()
        if sdk is None:
            self._emit(
                event_sink,
                request,
                "sdk_stream_error",
                {"error_type": "OuterLeaderSdkDependencyMissing", "package": SDK_PACKAGE},
                status="blocked",
            )
            return _blocked_result(
                "OuterLeaderSdkDependencyMissing",
                "Install claude-agent-sdk to enable the long-lived outer leader SDK session.",
                request,
                handled_by=self.name,
            )

        client = await self._ensure_client(sdk, request)
        prompt = _build_user_prompt(request)
        query_session_id = _outer_claude_query_session_id(request)
        start_payload = {
            "session_id": query_session_id,
            "main_session_id": request.get("main_session_id"),
            "outer_claude_session_id": _outer_claude_native_session_id(request) or None,
            "input_id": request.get("input_id"),
        }
        if self._options_diagnostics:
            start_payload["outer_leader_options"] = self._options_diagnostics
            if self._options_diagnostics.get("settings_diagnostics"):
                start_payload["settings_diagnostics"] = self._options_diagnostics["settings_diagnostics"]
        self._emit(
            event_sink,
            request,
            "sdk_stream_started",
            start_payload,
        )
        try:
            await client.query(prompt, session_id=query_session_id)
        except TypeError:
            await client.query(prompt)

        messages: list[dict[str, Any]] = []
        result_message: dict[str, Any] | None = None
        async for message in client.receive_response():
            record = _message_record(message, request)
            messages.append(record)
            self._emit(
                event_sink,
                request,
                record["event_type"],
                record,
                status=record.get("status") or "streaming",
            )
            if record.get("sdk_message_type") == "ResultMessage":
                result_message = record

        self._emit(
            event_sink,
            request,
            "sdk_stream_final",
            {
                "session_id": query_session_id,
                "main_session_id": request.get("main_session_id"),
                "outer_claude_session_id": _outer_claude_native_session_id(request) or None,
                "message_count": len(messages),
            },
            status="completed",
        )
        return _sdk_result(request, messages, result_message, handled_by=self.name)

    async def _ensure_client(self, sdk: Any, request: dict[str, Any]) -> Any:
        if self._client is not None:
            return self._client
        client_cls = getattr(sdk, "ClaudeSDKClient", None)
        if client_cls is None:
            raise RuntimeError("claude_agent_sdk does not expose ClaudeSDKClient")
        options = self._build_options(sdk, request)
        client = client_cls(options=options)
        if hasattr(client, "connect"):
            await client.connect()
        elif hasattr(client, "__aenter__"):
            await client.__aenter__()
        else:
            raise RuntimeError("ClaudeSDKClient exposes neither connect() nor async context entry")
        self._client = client
        return client

    def _build_options(self, sdk: Any, request: dict[str, Any]) -> Any:
        options_cls = getattr(sdk, "ClaudeAgentOptions", None)
        if options_cls is None:
            raise RuntimeError("claude_agent_sdk does not expose ClaudeAgentOptions")
        control_root = Path(self.config.control_root)
        repo_root = Path(self.config.repo_root) if self.config.repo_root else Path.cwd()
        leader_prompt = _leader_prompt(control_root)
        leader_model = _leader_model(control_root)
        tools = _outer_leader_tools()
        allowed_tools = _outer_leader_allowed_tools(tools)
        cli_info = _outer_leader_cli_info(control_root, repo_root)
        settings_path = _outer_leader_settings_path(control_root, cli_info, repo_root)
        env = {
            "CLAUDE_CONTROL_ROOT": str(control_root),
            "BRIDGE_RUNTIME_REPO_KEY": str(request.get("repo_key") or ""),
            "BRIDGE_RUN_ID": str(request.get("run_id") or ""),
            "BRIDGE_MAIN_SESSION_ID": str(request.get("main_session_id") or ""),
            "CLAUDE_CONTROL_RUN_ID": str(request.get("run_id") or ""),
            "CLAUDE_CONTROL_MAIN_SESSION_ID": str(request.get("main_session_id") or ""),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        outer_claude_session_id = _outer_claude_native_session_id(request)
        if outer_claude_session_id:
            env["BRIDGE_OUTER_CLAUDE_SESSION_ID"] = outer_claude_session_id
            env["CLAUDE_CONTROL_OUTER_CLAUDE_SESSION_ID"] = outer_claude_session_id
        env.update(_bridge_process_env_overrides(control_root, cli_info, repo_root))
        env.update(_settings_env(settings_path))
        env.update(cli_info.get("env") or {})
        _ensure_loopback_provider_no_proxy(env)
        _ensure_env_api_key_alias(env)
        env["ANTHROPIC_MODEL"] = leader_model
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = leader_model
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = leader_model
        system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": leader_prompt,
            "exclude_dynamic_sections": True,
        }
        option_values = {
            "system_prompt": system_prompt,
            "cwd": str(repo_root),
            "tools": tools,
            "mcp_servers": cli_info.get("mcp_config") or _bridge_mcp_servers(control_root),
            "strict_mcp_config": cli_info.get("strict_mcp_config", True),
            "allowed_tools": allowed_tools,
            "disallowed_tools": _outer_leader_disallowed_tools(),
            "permission_mode": _outer_leader_permission_mode(),
            "model": leader_model,
            "cli_path": cli_info.get("cli_path"),
            "settings": str(settings_path) if settings_path and not _settings_loaded_by_home(cli_info) else None,
            "setting_sources": _outer_leader_setting_sources(cli_info),
            "include_partial_messages": True,
            "max_turns": _env_int("OUTER_LEADER_MAX_TURNS"),
            "max_budget_usd": _env_float("OUTER_LEADER_MAX_BUDGET_USD"),
            "env": env,
            "extra_args": cli_info.get("extra_args") or {},
        }
        self._options_diagnostics = _options_diagnostics(option_values, settings_path, cli_info)
        return _construct_options(
            options_cls,
            option_values,
        )

    def _load_sdk(self) -> Any | None:
        if self._sdk is not None:
            return self._sdk
        try:
            self._sdk = importlib.import_module(SDK_PACKAGE)
        except ModuleNotFoundError:
            return None
        return self._sdk

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None:
            return self._loop

        def run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._loop_ready.set()
            loop.run_forever()

        self._thread = threading.Thread(target=run_loop, name="runbridge-outer-sdk", daemon=True)
        self._thread.start()
        if not self._loop_ready.wait(timeout=5):
            raise RuntimeError("outer SDK event loop did not start")
        if self._loop is None:
            raise RuntimeError("outer SDK event loop missing")
        return self._loop

    def _emit(
        self,
        event_sink: OuterLeaderEventSink | None,
        request: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
        *,
        status: str = "streaming",
    ) -> None:
        if event_sink is None:
            return
        self._sequence += 1
        event_sink(event_type, payload, status=status, sequence=self._sequence)


def _bridge_mcp_servers(control_root: Path) -> dict[str, dict[str, Any]]:
    bridge_env = {
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "CLAUDE_CONTROL_ROOT": str(control_root),
    }
    bridge_env.update(_bridge_process_env_overrides(control_root))
    return {
        "bridge": {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(control_root / "mcp" / "bridge_server.py")],
            "env": bridge_env,
        }
    }


def _bridge_process_env_overrides(control_root: Path, cli_info: dict[str, Any] | None = None, repo_root: Path | None = None) -> dict[str, str]:
    keys = (
        "BRIDGE_CLAUDE_COMMAND",
        "BRIDGE_CLAUDE_CLI",
        "BRIDGE_CLAUDE_SETTINGS",
        "BRIDGE_DISABLE_CLAUDE_MJY_AUTO",
    )
    overrides = {key: value for key in keys if (value := os.environ.get(key))}
    cli_info = cli_info or _default_outer_leader_cli_info(control_root, repo_root)
    if "BRIDGE_CLAUDE_COMMAND" not in overrides and cli_info.get("bridge_claude_command"):
        overrides["BRIDGE_CLAUDE_COMMAND"] = str(cli_info["bridge_claude_command"])
    return overrides


def _outer_leader_settings_path(control_root: Path, cli_info: dict[str, Any] | None = None, repo_root: Path | None = None) -> Path | None:
    explicit = os.environ.get("OUTER_LEADER_CLAUDE_SETTINGS") or os.environ.get("BRIDGE_CLAUDE_SETTINGS")
    if explicit:
        return Path(explicit).expanduser().resolve()

    cli_settings = (cli_info or {}).get("settings")
    if cli_settings:
        return Path(str(cli_settings)).expanduser().resolve()

    claude_root = _discover_parent_claude_root(control_root, repo_root)
    default_settings = claude_root / "settings.json"
    hook_settings = claude_root / "hooks" / "settings.json"
    if default_settings.exists() or hook_settings.exists():
        source = default_settings if default_settings.exists() else hook_settings
        return _materialize_outer_leader_settings(control_root, source, hook_settings=hook_settings, claude_root=claude_root)

    return None


def _materialize_outer_leader_settings(
    control_root: Path,
    source: Path,
    *,
    hook_settings: Path | None = None,
    claude_root: Path | None = None,
) -> Path:
    source = source.expanduser().resolve()
    claude_root = (claude_root.expanduser().resolve() if claude_root else (source.parent.parent if source.parent.name == "hooks" else source.parent))
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid Claude settings payload: {source}")
    if hook_settings and hook_settings.exists():
        hook_payload = json.loads(hook_settings.read_text(encoding="utf-8"))
        if isinstance(hook_payload, dict) and isinstance(hook_payload.get("hooks"), dict):
            payload = {**payload, "hooks": _filter_claude_cli_hooks(hook_payload["hooks"])}
    normalized = _normalize_hook_commands(payload, claude_root / "hooks")
    _ensure_settings_loopback_provider_no_proxy(normalized)
    _ensure_settings_api_key_alias(normalized)
    target = claude_root / "runtime_state" / "generated" / "outer_leader_settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return target.resolve()


def _ensure_settings_api_key_alias(settings_payload: dict[str, Any]) -> bool:
    env = settings_payload.get("env")
    if not isinstance(env, dict):
        return False
    if _has_nonempty(env.get("ANTHROPIC_API_KEY")):
        return False
    auth_token = env.get("ANTHROPIC_AUTH_TOKEN")
    if not _has_nonempty(auth_token):
        return False
    env["ANTHROPIC_API_KEY"] = str(auth_token)
    return True


def _ensure_env_api_key_alias(env: dict[str, str]) -> bool:
    if _has_nonempty(env.get("ANTHROPIC_API_KEY")):
        return False
    auth_token = env.get("ANTHROPIC_AUTH_TOKEN")
    if not _has_nonempty(auth_token):
        return False
    env["ANTHROPIC_API_KEY"] = str(auth_token)
    return True


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


def _normalize_hook_commands(value: Any, hooks_root: Path) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_hook_commands(item, hooks_root) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_hook_commands(item, hooks_root) for item in value]
    if isinstance(value, str):
        return _absolute_hook_command(value, hooks_root)
    return value


def _absolute_hook_command(command: str, hooks_root: Path) -> str:
    normalized = command.replace("\\", "/")
    match = None
    for pattern in ("../.claude/hooks/", ".claude/hooks/"):
        index = normalized.find(pattern)
        if index >= 0:
            tail = normalized[index + len(pattern) :].strip()
            script = tail.split()[0] if tail else ""
            if script:
                match = hooks_root / script
                break
    if match is None:
        return command
    return f"{_quote_cmd_arg(sys.executable)} {_quote_cmd_arg(str(match))}"


def _quote_cmd_arg(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def _settings_env(settings_path: Path | None) -> dict[str, str]:
    payload = _read_settings(settings_path)
    env = payload.get("env") if isinstance(payload, dict) else None
    if not isinstance(env, dict):
        return {}
    return {str(key): str(value) for key, value in env.items() if value is not None}


def _read_settings(settings_path: Path | None) -> dict[str, Any]:
    if not settings_path or not settings_path.exists():
        return {}
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _infer_outer_settings_source(settings_path: Path | None) -> str | None:
    if not settings_path:
        return None
    generated = (settings_path.parent / "outer_leader_settings.json").resolve()
    if settings_path.resolve() != generated:
        return str(settings_path)
    claude_root = settings_path.parent.parent.parent
    default_settings = claude_root / "settings.json"
    hook_settings = claude_root / "hooks" / "settings.json"
    if default_settings.exists() and hook_settings.exists():
        return f"{default_settings.resolve()} + {hook_settings.resolve()}"
    if hook_settings.exists():
        return str(hook_settings.resolve())
    if default_settings.exists():
        return str(default_settings.resolve())
    return str(settings_path)


def _outer_leader_permission_mode() -> str | None:
    raw = os.environ.get("OUTER_LEADER_PERMISSION_MODE")
    if raw is None:
        return DEFAULT_PERMISSION_MODE
    return raw.strip() or None


def _outer_leader_cli_info(control_root: Path, repo_root: Path | None = None) -> dict[str, Any]:
    for name in ("OUTER_LEADER_CLAUDE_CLI", "BRIDGE_CLAUDE_CLI"):
        value = os.environ.get(name)
        if value and value.strip():
            default_info = _default_outer_leader_cli_info(control_root, repo_root)
            return {
                "cli_path": _resolve_cli_path(value.strip()),
                "cli_source": name,
                "cli_warning": None,
                "env": {},
                "mcp_config": default_info.get("mcp_config"),
                "settings": None,
                "extra_args": {},
                "strict_mcp_config": True,
            }

    command = os.environ.get("BRIDGE_CLAUDE_COMMAND")
    if command and command.strip():
        parsed = _parse_claude_command(command)
        if parsed.get("command"):
            default_info = _default_outer_leader_cli_info(control_root, repo_root)
            return {
                "cli_path": _resolve_cli_path(str(parsed["command"]), parsed.get("env") or {}),
                "cli_source": "BRIDGE_CLAUDE_COMMAND",
                "cli_warning": parsed.get("warning"),
                "env": parsed.get("env") or {},
                "mcp_config": parsed.get("mcp_config") or default_info.get("mcp_config"),
                "settings": parsed.get("settings"),
                "extra_args": parsed.get("extra_args") or {},
                "strict_mcp_config": parsed.get("strict_mcp_config", True),
                "raw_args": parsed.get("raw_args") or [],
                "ignored_args": parsed.get("ignored_args") or [],
            }
        return {
            "cli_path": None,
            "cli_source": "BRIDGE_CLAUDE_COMMAND",
            "cli_warning": parsed.get("warning") or "command_not_parseable",
            "env": parsed.get("env") or {},
            "mcp_config": parsed.get("mcp_config"),
            "settings": parsed.get("settings"),
            "extra_args": parsed.get("extra_args") or {},
            "strict_mcp_config": parsed.get("strict_mcp_config", True),
            "raw_args": parsed.get("raw_args") or [],
            "ignored_args": parsed.get("ignored_args") or [],
        }

    default_info = _default_outer_leader_cli_info(control_root, repo_root)
    if default_info.get("mcp_config"):
        return default_info

    if os.environ.get("BRIDGE_DISABLE_CLAUDE_MJY_AUTO", "").strip().lower() not in {"1", "true", "yes"}:
        preferred = shutil.which("claude_mjy")
        if preferred:
            default_info = _default_outer_leader_cli_info(control_root, repo_root)
            return {
                "cli_path": str(Path(preferred).expanduser()),
                "cli_source": "PATH:claude_mjy",
                "cli_warning": None,
                "env": {},
                "mcp_config": default_info.get("mcp_config"),
                "settings": None,
                "extra_args": {},
                "strict_mcp_config": True,
            }

    return {
        "cli_path": None,
        "cli_source": "default",
        "cli_warning": None,
        "env": {},
        "mcp_config": None,
        "settings": None,
        "extra_args": {},
        "strict_mcp_config": True,
    }


def _default_outer_leader_cli_info(control_root: Path, repo_root: Path | None = None) -> dict[str, Any]:
    claude_root = _discover_parent_claude_root(control_root, repo_root)
    workspace_home = claude_root.parent
    mcp_config = claude_root / "mcp.json"
    if not mcp_config.exists() or os.environ.get("BRIDGE_DISABLE_CLAUDE_STARTUP_DEFAULTS", "").strip().lower() in {"1", "true", "yes"}:
        return {
            "cli_path": None,
            "cli_source": "default",
            "cli_warning": None,
            "env": {},
            "mcp_config": None,
            "settings": None,
            "extra_args": {},
            "strict_mcp_config": True,
        }
    command = f"HOME={_shell_quote(str(workspace_home))} claude --mcp-config {_shell_quote(str(mcp_config))}"
    return {
        "cli_path": _resolve_cli_path("claude", {"HOME": str(workspace_home)}),
        "cli_source": "control_root_default",
        "cli_warning": None,
        "env": {"HOME": str(workspace_home)},
        "mcp_config": str(mcp_config),
        "settings": None,
        "extra_args": {},
        "strict_mcp_config": True,
        "bridge_claude_command": command,
    }


def _discover_parent_claude_root(control_root: Path, repo_root: Path | None = None) -> Path:
    if repo_root:
        candidate = Path(repo_root).expanduser().resolve().parent / ".claude"
        if candidate.exists():
            return candidate
    return Path(control_root).expanduser().resolve().parent


def _shell_quote(value: str) -> str:
    if os.name != "nt":
        return shlex.quote(value)
    if not value or any(ch.isspace() for ch in value) or '"' in value:
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _parse_claude_command(command: str) -> dict[str, Any]:
    try:
        parts = shlex.split(command, posix=(os.name != "nt"))
    except ValueError as exc:
        return {"command": command.strip(), "env": {}, "warning": f"command_parse_failed:{type(exc).__name__}"}

    env: dict[str, str] = {}
    while parts and _is_env_assignment(parts[0]):
        key, value = parts.pop(0).split("=", 1)
        env[key] = value

    if not parts:
        return {"command": None, "env": env, "warning": "command_missing_after_env_assignments"}

    executable = parts.pop(0)
    parsed_args = _parse_sdk_compatible_cli_args(parts)
    warning = parsed_args.get("warning")
    if parsed_args.get("ignored_args") and not warning:
        warning = "unsupported_bridge_claude_command_args_ignored"
    return {
        "command": executable,
        "env": env,
        **parsed_args,
        "warning": warning,
    }


def _parse_sdk_compatible_cli_args(parts: list[str]) -> dict[str, Any]:
    mcp_config: str | None = None
    settings: str | None = None
    strict_mcp_config = True
    extra_args: dict[str, str | None] = {}
    ignored_args: list[str] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if part == "--mcp-config":
            if index + 1 < len(parts):
                mcp_config = parts[index + 1]
                index += 2
                continue
            ignored_args.append(part)
            index += 1
            continue
        if part.startswith("--mcp-config="):
            mcp_config = part.split("=", 1)[1]
            index += 1
            continue
        if part == "--settings":
            if index + 1 < len(parts):
                settings = parts[index + 1]
                index += 2
                continue
            ignored_args.append(part)
            index += 1
            continue
        if part.startswith("--settings="):
            settings = part.split("=", 1)[1]
            index += 1
            continue
        if part == "--strict-mcp-config":
            strict_mcp_config = True
            index += 1
            continue
        if part.startswith("--") and index + 1 < len(parts) and not parts[index + 1].startswith("--"):
            extra_args[part[2:]] = parts[index + 1]
            index += 2
            continue
        if part.startswith("--"):
            extra_args[part[2:]] = None
            index += 1
            continue
        ignored_args.append(part)
        index += 1
    return {
        "mcp_config": _expand_cli_path(mcp_config),
        "settings": _expand_cli_path(settings),
        "strict_mcp_config": strict_mcp_config,
        "extra_args": extra_args,
        "raw_args": parts,
        "ignored_args": ignored_args,
    }


def _is_env_assignment(value: str) -> bool:
    key, sep, _rest = value.partition("=")
    if sep != "=" or not key:
        return False
    if not (key[0].isalpha() or key[0] == "_"):
        return False
    return all(ch.isalnum() or ch == "_" for ch in key)


def _expand_cli_path(value: str | None) -> str | None:
    if not value:
        return None
    if os.name == "nt" and value.startswith("/"):
        return value
    return str(Path(value).expanduser()) if any(sep in value for sep in ("/", "\\")) else value


def _resolve_cli_path(value: str, env: dict[str, str] | None = None) -> str:
    expanded = Path(value).expanduser()
    if expanded.is_absolute() or any(sep in value for sep in ("/", "\\")):
        return str(expanded)
    if value == "claude":
        env_home = (env or {}).get("HOME")
        if env_home:
            resolved = _resolve_claude_from_home(env_home)
            if resolved:
                return resolved
    resolved = shutil.which(value)
    if resolved:
        return resolved
    if value == "claude":
        process_home = os.environ.get("HOME")
        if process_home and process_home != (env or {}).get("HOME"):
            resolved = _resolve_claude_from_home(process_home)
            if resolved:
                return resolved
    return value


def _resolve_claude_from_home(home: str) -> str | None:
    for relative in (".local/bin/claude", ".npm-global/bin/claude"):
        candidate = Path(home).expanduser() / relative
        if candidate.exists():
            return str(candidate)
    return None


def _outer_leader_setting_sources(cli_info: dict[str, Any]) -> list[str]:
    raw = os.environ.get("OUTER_LEADER_SETTING_SOURCES")
    if raw is not None:
        raw = raw.strip()
        return [item.strip() for item in raw.split(",") if item.strip()] if raw else []
    if (cli_info.get("env") or {}).get("HOME") or cli_info.get("mcp_config"):
        return ["user"]
    return []


def _settings_loaded_by_home(cli_info: dict[str, Any]) -> bool:
    return bool((cli_info.get("env") or {}).get("HOME") and not cli_info.get("settings"))


def _options_diagnostics(values: dict[str, Any], settings_path: Path | None, cli_info: dict[str, Any] | None = None) -> dict[str, Any]:
    env = values.get("env") if isinstance(values.get("env"), dict) else {}
    settings_env = _settings_env(settings_path)
    cli_info = cli_info or {}
    cli_home = (cli_info.get("env") or {}).get("HOME")
    cli_mcp_config = cli_info.get("mcp_config")
    return {
        "tools": list(values.get("tools") or []),
        "allowed_tools": list(values.get("allowed_tools") or []),
        "disallowed_tools": list(values.get("disallowed_tools") or []),
        "permission_mode": values.get("permission_mode"),
        "model": values.get("model"),
        "cli_path": values.get("cli_path"),
        "cli_source": cli_info.get("cli_source"),
        "cli_warning": cli_info.get("cli_warning"),
        "cli_env_keys": sorted((cli_info.get("env") or {}).keys()),
        "cli_home": str(cli_home) if cli_home else None,
        "cli_mcp_config": cli_info.get("mcp_config"),
        "cli_mcp_config_exists": _path_exists(cli_mcp_config),
        "cli_extra_args": cli_info.get("extra_args") or {},
        "cli_ignored_args": cli_info.get("ignored_args") or [],
        "bridge_claude_command": _safe_command_preview(os.environ.get("BRIDGE_CLAUDE_COMMAND") or cli_info.get("bridge_claude_command")),
        "fallback_model": values.get("fallback_model"),
        "settings": values.get("settings"),
        "setting_sources": list(values.get("setting_sources") or []),
        "settings_diagnostics": {
            "settings_path": str(settings_path) if settings_path else None,
            "settings_path_exists": bool(settings_path and settings_path.exists()),
            "inferred_source_path": _infer_outer_settings_source(settings_path),
            "settings_env_keys": sorted(settings_env.keys()),
            "settings_has_anthropic_base_url": _has_nonempty(settings_env.get("ANTHROPIC_BASE_URL")),
            "settings_anthropic_base_url": _safe_url_preview(settings_env.get("ANTHROPIC_BASE_URL")),
            "settings_has_anthropic_auth_token": _has_nonempty(settings_env.get("ANTHROPIC_AUTH_TOKEN")),
            "settings_has_anthropic_api_key": _has_nonempty(settings_env.get("ANTHROPIC_API_KEY")),
            "settings_auth_token_aliased_to_api_key": _has_nonempty(settings_env.get("ANTHROPIC_AUTH_TOKEN"))
            and settings_env.get("ANTHROPIC_API_KEY") == settings_env.get("ANTHROPIC_AUTH_TOKEN"),
            "settings_has_http_proxy": _has_nonempty(_env_value_ci(settings_env, "HTTP_PROXY")),
            "settings_has_https_proxy": _has_nonempty(_env_value_ci(settings_env, "HTTPS_PROXY")),
            "settings_has_no_proxy": _has_nonempty(_env_value_ci(settings_env, "NO_PROXY")),
            "settings_no_proxy_has_loopback": _no_proxy_includes_loopback(settings_env),
            "subprocess_env_has_anthropic_base_url": _has_nonempty(env.get("ANTHROPIC_BASE_URL")),
            "subprocess_anthropic_base_url": _safe_url_preview(env.get("ANTHROPIC_BASE_URL")),
            "subprocess_env_has_anthropic_auth_token": _has_nonempty(env.get("ANTHROPIC_AUTH_TOKEN")),
            "subprocess_env_has_anthropic_api_key": _has_nonempty(env.get("ANTHROPIC_API_KEY")),
            "subprocess_env_auth_token_aliased_to_api_key": _has_nonempty(env.get("ANTHROPIC_AUTH_TOKEN"))
            and env.get("ANTHROPIC_API_KEY") == env.get("ANTHROPIC_AUTH_TOKEN"),
            "subprocess_anthropic_model": env.get("ANTHROPIC_MODEL"),
            "subprocess_default_sonnet_model": env.get("ANTHROPIC_DEFAULT_SONNET_MODEL"),
            "subprocess_default_haiku_model": env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
            "subprocess_env_has_http_proxy": _has_nonempty(_env_value_ci(env, "HTTP_PROXY")),
            "subprocess_env_has_https_proxy": _has_nonempty(_env_value_ci(env, "HTTPS_PROXY")),
            "subprocess_env_has_no_proxy": _has_nonempty(_env_value_ci(env, "NO_PROXY")),
            "subprocess_no_proxy_has_loopback": _no_proxy_includes_loopback(env),
        },
    }


def outer_leader_startup_diagnostics(
    control_root: str | Path,
    repo_root: str | Path | None = None,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the outer leader startup plan without sending an API request."""

    control = Path(control_root).expanduser().resolve()
    repo = Path(repo_root).expanduser().resolve() if repo_root else Path.cwd().resolve()
    request = dict(request or {})
    leader_model = _leader_model(control)
    tools = _outer_leader_tools()
    allowed_tools = _outer_leader_allowed_tools(tools)
    cli_info = _outer_leader_cli_info(control, repo)
    settings_path = _outer_leader_settings_path(control, cli_info, repo)
    env = {
        "CLAUDE_CONTROL_ROOT": str(control),
        "BRIDGE_RUNTIME_REPO_KEY": str(request.get("repo_key") or ""),
        "BRIDGE_RUN_ID": str(request.get("run_id") or ""),
        "BRIDGE_MAIN_SESSION_ID": str(request.get("main_session_id") or ""),
        "CLAUDE_CONTROL_RUN_ID": str(request.get("run_id") or ""),
        "CLAUDE_CONTROL_MAIN_SESSION_ID": str(request.get("main_session_id") or ""),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    outer_claude_session_id = _outer_claude_native_session_id(request)
    if outer_claude_session_id:
        env["BRIDGE_OUTER_CLAUDE_SESSION_ID"] = outer_claude_session_id
        env["CLAUDE_CONTROL_OUTER_CLAUDE_SESSION_ID"] = outer_claude_session_id
    env.update(_bridge_process_env_overrides(control, cli_info, repo))
    env.update(_settings_env(settings_path))
    env.update(cli_info.get("env") or {})
    _ensure_loopback_provider_no_proxy(env)
    _ensure_env_api_key_alias(env)
    env["ANTHROPIC_MODEL"] = leader_model
    env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = leader_model
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = leader_model
    option_values = {
        "cwd": str(repo),
        "tools": tools,
        "mcp_servers": cli_info.get("mcp_config") or _bridge_mcp_servers(control),
        "strict_mcp_config": cli_info.get("strict_mcp_config", True),
        "allowed_tools": allowed_tools,
        "disallowed_tools": _outer_leader_disallowed_tools(),
        "permission_mode": _outer_leader_permission_mode(),
        "model": leader_model,
        "cli_path": cli_info.get("cli_path"),
        "settings": str(settings_path) if settings_path and not _settings_loaded_by_home(cli_info) else None,
        "setting_sources": _outer_leader_setting_sources(cli_info),
        "include_partial_messages": True,
        "max_turns": _env_int("OUTER_LEADER_MAX_TURNS"),
        "max_budget_usd": _env_float("OUTER_LEADER_MAX_BUDGET_USD"),
        "env": env,
        "extra_args": cli_info.get("extra_args") or {},
    }
    effective = _options_diagnostics(option_values, settings_path, cli_info)
    settings_diag = effective.get("settings_diagnostics", {})
    checks = {
        "home_env_present": bool(effective.get("cli_home")),
        "mcp_config_present": bool(effective.get("cli_mcp_config")),
        "mcp_config_exists": bool(effective.get("cli_mcp_config_exists")),
        "settings_arg_mode": "flag" if effective.get("settings") else "home",
        "setting_sources_user_only": effective.get("setting_sources") == ["user"],
        "process_env_provider_overrides": bool(
            settings_diag.get("subprocess_env_has_anthropic_base_url")
            or settings_diag.get("subprocess_env_has_anthropic_auth_token")
        ),
        "settings_file_provider_env_present": bool(
            settings_diag.get("settings_has_anthropic_base_url")
            or settings_diag.get("settings_has_anthropic_auth_token")
        ),
        "settings_provider_env_propagated": (
            not (
                settings_diag.get("settings_has_anthropic_base_url")
                or settings_diag.get("settings_has_anthropic_auth_token")
            )
            or (
                settings_diag.get("subprocess_env_has_anthropic_base_url")
                and settings_diag.get("subprocess_env_has_anthropic_auth_token")
            )
        ),
    }
    return {
        "schema_version": "outer_leader_startup_diagnostics.v1",
        "control_root": str(control),
        "repo_root": str(repo),
        "parent_claude_root": str(_discover_parent_claude_root(control, repo)),
        "effective_options": effective,
        "checks": checks,
        "verdict": _startup_diagnostic_verdict(checks),
    }


def _startup_diagnostic_verdict(checks: dict[str, Any]) -> dict[str, Any]:
    problems: list[str] = []
    warnings: list[str] = []
    if checks.get("settings_file_provider_env_present") and not checks.get("settings_provider_env_propagated"):
        problems.append("settings provider env is not present in the actual outer leader subprocess environment")
    if not checks.get("home_env_present"):
        warnings.append("HOME is not set by the Claude startup wrapper")
    if not checks.get("mcp_config_present"):
        warnings.append("no --mcp-config path is active")
    elif not checks.get("mcp_config_exists"):
        problems.append("active --mcp-config path does not exist")
    if checks.get("settings_arg_mode") == "flag":
        warnings.append("SDK will pass --settings instead of relying on HOME/user settings")
    if not checks.get("setting_sources_user_only"):
        warnings.append("setting_sources is not exactly ['user']")
    if problems:
        status = "fail"
    elif warnings:
        status = "warn"
    else:
        status = "ok"
    return {"status": status, "problems": problems, "warnings": warnings}


def _construct_options(options_cls: Any, values: dict[str, Any]) -> Any:
    kwargs = {key: value for key, value in values.items() if value is not None}
    try:
        signature = inspect.signature(options_cls)
        allowed = set(signature.parameters)
        if "kwargs" not in allowed and not any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
            kwargs = {key: value for key, value in kwargs.items() if key in allowed}
    except (TypeError, ValueError):
        pass
    return options_cls(**kwargs)


def _leader_prompt(control_root: Path) -> str:
    path = control_root.parent / "agents" / "leader-orchestrator.md"
    if not path.exists():
        return "You are leader-orchestrator. Use runtime truth and bridge MCP tools as the control path."
    return _strip_frontmatter(path.read_text(encoding="utf-8", errors="replace")).strip()


def _leader_model(control_root: Path) -> str:
    explicit = os.environ.get("OUTER_LEADER_MODEL")
    if explicit and explicit.strip():
        return explicit.strip()
    path = control_root.parent / "agents" / "leader-orchestrator.md"
    if path.exists():
        frontmatter = _frontmatter(path.read_text(encoding="utf-8-sig", errors="replace"))
        model = str(frontmatter.get("model") or "").strip()
        if model:
            return model
    return os.environ.get("BRIDGE_FALLBACK_MODEL", "gpt-main").strip() or "gpt-main"


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    marker = text.find("\n---", 3)
    if marker == -1:
        return {}
    data: dict[str, str] = {}
    for line in text[3:marker].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip()
    return data


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    marker = text.find("\n---", 3)
    if marker == -1:
        return text
    return text[marker + 4 :].lstrip()


def _build_user_prompt(request: dict[str, Any]) -> str:
    text = str(request.get("text") or "")
    if str(request.get("dispatch_intent") or "").strip() == "leader_decide":
        envelope = {
            "dispatch_intent": "leader_decide",
            "repo_key": request.get("repo_key"),
            "run_id": request.get("run_id"),
            "input_id": request.get("input_id"),
            "input_kind": request.get("input_kind"),
            "target_phase": request.get("target_phase"),
            "user_text": text.strip(),
            "contract": [
                "Read runtime truth before deciding when state matters.",
                "Make the semantic judgment yourself; do not rely on keyword phase matching.",
                "If you decide to open a bridge, pass your chosen target_phase explicitly to mcp__bridge__build_bridge_packet.",
                "If the user intent moves target work forward, call mcp__bridge__build_bridge_packet and then mcp__bridge__call_bridge_sdk in this turn.",
                "If no bridge should open, answer concisely and include NO_BRIDGE_DECISION: <semantic reason>.",
            ],
        }
        return "Handle this 8787 operator input under the leader_decide contract:\n" + json.dumps(
            envelope,
            ensure_ascii=False,
        )
    return text


def _message_record(message: Any, request: dict[str, Any]) -> dict[str, Any]:
    message_type = type(message).__name__
    payload = _to_jsonable(message)
    preview = _message_preview(payload, message_type)
    text = _message_text(payload)
    event_type = _event_type(message_type, payload)
    return {
        "timestamp": _now_iso(),
        "event_type": event_type,
        "source": "outer_sdk",
        "stream_source": "sdk",
        "raw_stream_event_type": message_type,
        "sdk_message_type": message_type,
        "run_id": request.get("run_id"),
        "repo_key": request.get("repo_key"),
        "session_id": request.get("main_session_id"),
        "agent_id": "leader-orchestrator",
        "agent_type": "main-leader",
        "status": "completed" if message_type == "ResultMessage" else "streaming",
        "message_preview": preview,
        "text_delta": text,
        "payload_keys": sorted(str(key) for key in payload.keys())[:20] if isinstance(payload, dict) else [],
        **_result_fields(payload),
        **_tool_fields(payload),
    }


def _event_type(message_type: str, payload: dict[str, Any]) -> str:
    if message_type == "ResultMessage":
        return "sdk_stream_final_result"
    if _tool_fields(payload):
        fields = _tool_fields(payload)
        return "sdk_stream_tool_result" if fields.get("tool_result_id") else "sdk_stream_tool_use"
    if _message_text(payload):
        return "sdk_stream_assistant_text"
    return "sdk_stream_delta"


def _tool_fields(payload: dict[str, Any]) -> dict[str, Any]:
    for block in _content_blocks(payload):
        block_type = str(block.get("type") or type(block).__name__)
        if block_type == "tool_use" or "ToolUse" in block_type:
            return {
                "tool_id": block.get("id"),
                "tool_name": block.get("name"),
                "tool_block_type": "tool_use",
                "tool_input_keys": sorted(str(key) for key in (block.get("input") or {}).keys())[:20]
                if isinstance(block.get("input"), dict)
                else [],
            }
        if block_type == "tool_result" or "ToolResult" in block_type:
            return {
                "tool_result_id": block.get("tool_use_id") or block.get("id"),
                "tool_block_type": "tool_result",
                "is_error": block.get("is_error"),
            }
    return {}


def _content_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    content = payload.get("content") if isinstance(payload, dict) else None
    if isinstance(content, list):
        return [item for item in (_to_jsonable(item) for item in content) if isinstance(item, dict)]
    return []


def _result_fields(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    fields: dict[str, Any] = {}
    for key in ("subtype", "result", "session_id", "total_cost_usd", "duration_ms", "num_turns", "stop_reason", "is_error"):
        if key in payload:
            value = payload.get(key)
            fields[key] = _safe_report_text(value) if key == "result" else value
    for key in ("permission_denials", "errors"):
        if key in payload:
            fields[key] = _to_jsonable(payload.get(key))
    return fields


def _message_text(payload: dict[str, Any]) -> str | None:
    parts: list[str] = []
    if isinstance(payload.get("text"), str):
        parts.append(payload["text"])
    for block in _content_blocks(payload):
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return _safe_preview("\n".join(parts)) if parts else None


def _message_preview(payload: dict[str, Any], message_type: str) -> str:
    parts = [message_type]
    text = _message_text(payload)
    if text:
        parts.append(text)
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, str) and result.strip():
        parts.append(result.strip())
    subtype = payload.get("subtype") if isinstance(payload, dict) else None
    if isinstance(subtype, str) and subtype.strip():
        parts.append(subtype.strip())
    if len(parts) == 1:
        tool = _tool_fields(payload)
        if tool:
            parts.append(json.dumps(tool, ensure_ascii=False, separators=(",", ":")))
    return _safe_preview("\n".join(parts))


def _sdk_result(
    request: dict[str, Any],
    messages: list[dict[str, Any]],
    result_message: dict[str, Any] | None,
    *,
    handled_by: str,
) -> dict[str, Any]:
    subtype = result_message.get("subtype") if result_message else None
    summary = _result_text(result_message) or _last_result_text(messages) or _last_preview(messages) or "outer leader SDK response completed"
    contract_violation = _outer_leader_contract_violation(summary, result_message) or _outer_leader_message_contract_violation(request, messages, summary)
    system_failure = _outer_leader_system_failure(summary, result_message)
    status = (
        "blocked"
        if contract_violation
        else "failed"
        if system_failure
        else ("succeeded" if subtype in {None, "success"} else "failed")
    )
    return {
        "status": status,
        "handled_by": handled_by,
        "reports": [
            {
                "summary": summary,
                "source": "outer_leader_sdk",
                "session_id": _outer_claude_query_session_id(request),
                "main_session_id": request.get("main_session_id"),
                "outer_claude_session_id": _outer_claude_native_session_id(request) or None,
                "message_count": len(messages),
            }
        ],
        "artifact_refs": [],
        "evidence": {
            "outer_sdk_session_id": _outer_claude_query_session_id(request),
            "main_session_id": request.get("main_session_id"),
            "outer_claude_session_id": _outer_claude_native_session_id(request) or None,
            "sdk_message_count": len(messages),
            "result_subtype": subtype,
            "runtime_event_id": request.get("runtime_event_id"),
            "permission_denials": result_message.get("permission_denials") if isinstance(result_message, dict) else None,
            "contract_violation": contract_violation,
            "system_failure": system_failure,
        },
        "error_or_null": None
        if status == "succeeded"
        else (
            {
                "type": "OuterLeaderContractViolation",
                "message": contract_violation,
            }
            if contract_violation
            else (
                system_failure
                if system_failure
                else {
                "type": "OuterLeaderSdkResultNotSuccess",
                "message": str(subtype or "SDK response ended without a success result"),
                }
            )
        ),
        "cleanup_required": False,
    }


def _outer_leader_contract_violation(summary: str, result_message: dict[str, Any] | None = None) -> str | None:
    permission_denials = result_message.get("permission_denials") if isinstance(result_message, dict) else None
    if permission_denials:
        return "Outer leader hit tool permission denials instead of using the approved bridge control path."

    text = str(summary or "")
    lowered = text.lower()
    asks_for_mcp_auth = (
        "mcp__" in text
        and (
            "\u6388\u6743" in text
            or "\u5141\u8bb8" in text
            or "permission" in lowered
            or "allow" in lowered
        )
    )
    if asks_for_mcp_auth:
        return "Outer leader asked the user to authorize MCP tools instead of reporting a workflow-system failure."
    if "mcp__codex__codex" in text:
        return "Outer leader attempted to delegate implementation through Codex MCP instead of L4 bridge routing."
    if "dispatch_workflow_event" in text and (
        "\u5f00\u653e" in text or "\u6388\u6743" in text or "reroute_phase" in text
    ):
        return "Outer leader attempted to mutate workflow routing directly instead of using build_bridge_packet/call_bridge_sdk."
    if "\u4f60\u9700\u8981\u624b\u52a8\u6267\u884c" in text or "\u6211\u5f53\u524d\u7684\u5de5\u5177\u6743\u9650\u65e0\u6cd5\u76f4\u63a5\u7f16\u8f91" in text:
        return "Outer leader asked the user to perform manual implementation instead of routing to L4 bridge."
    return None


def _outer_leader_message_contract_violation(request: dict[str, Any], messages: list[dict[str, Any]], summary: str) -> str | None:
    if _is_tool_artifact_filename(summary):
        return "Outer leader returned only a tool artifact filename instead of a runtime-backed report."
    reconcile_seen = False
    build_seen = False
    call_seen = False
    for item in messages:
        tool_name = str(item.get("tool_name") or "")
        if tool_name == "mcp__bridge__reconcile_workflow_from_ledger":
            reconcile_seen = True
        if tool_name == "mcp__bridge__build_bridge_packet":
            build_seen = True
        if tool_name == "mcp__bridge__call_bridge_sdk":
            call_seen = True
    if _is_advance_or_continue_request(request) and reconcile_seen and not build_seen and not call_seen:
        return "Outer leader reconciled workflow state for an advance/continue request but stopped before mcp__bridge__build_bridge_packet and mcp__bridge__call_bridge_sdk."
    if build_seen and not call_seen:
        return "Outer leader built a BridgePacket but stopped before mcp__bridge__call_bridge_sdk."
    return None


def _is_advance_or_continue_request(request: dict[str, Any]) -> bool:
    return str(request.get("dispatch_intent") or "").strip() == "advance_or_continue"


def _is_tool_artifact_filename(text: Any) -> bool:
    normalized = str(text or "").strip()
    normalized = re.sub(r"^[^\w./-]+", "", normalized)
    return bool(re.fullmatch(r"(?:br)?idge_packet-\d+\.txt", normalized))


def _outer_leader_system_failure(summary: str, result_message: dict[str, Any] | None = None) -> dict[str, str] | None:
    text = str(summary or "")
    if isinstance(result_message, dict):
        errors = result_message.get("errors")
        if errors:
            text = f"{text}\n{json.dumps(errors, ensure_ascii=False, default=str)}"
    lowered = text.lower()
    connection_refused = "connectionrefused" in lowered or "connection refused" in lowered or "econnrefused" in lowered
    api_connect_error = "api error" in lowered and ("connect" in lowered or "connection" in lowered)
    invalid_model = "api error" in lowered and "invalid model name" in lowered
    api_400 = "api error: 400" in lowered or "api error 400" in lowered
    if invalid_model:
        return {
            "type": "OuterLeaderSdkInvalidModel",
            "message": _safe_report_text(summary or "Outer leader SDK used a model name rejected by the configured LLM API."),
        }
    if api_400:
        return {
            "type": "OuterLeaderSdkApiRequestFailed",
            "message": _safe_report_text(summary or "Outer leader SDK request was rejected by the configured LLM API."),
        }
    if connection_refused or api_connect_error:
        return {
            "type": "OuterLeaderSdkApiConnectionFailed",
            "message": _safe_report_text(summary or "Outer leader SDK could not connect to the configured LLM API."),
        }
    return None


def _last_preview(messages: list[dict[str, Any]]) -> str | None:
    for item in reversed(messages):
        preview = item.get("message_preview")
        if preview:
            return str(preview)[:PREVIEW_LIMIT]
    return None


def _last_result_text(messages: list[dict[str, Any]]) -> str | None:
    for item in reversed(messages):
        text = _result_text(item)
        if text:
            return text
    return None


def _result_text(message: dict[str, Any] | None) -> str | None:
    if not isinstance(message, dict):
        return None
    value = message.get("result")
    if isinstance(value, str) and value.strip():
        return _safe_report_text(value)
    return None


def _blocked_result(error_type: str, message: str, request: dict[str, Any], *, handled_by: str) -> dict[str, Any]:
    return {
        "status": "blocked",
        "handled_by": handled_by,
        "reports": [],
        "artifact_refs": [],
        "evidence": {
            "outer_sdk_migration_point": "outer_sdk.claude_agent_adapter.ClaudeAgentSdkOuterLeaderAdapter",
            "run_id": request.get("run_id"),
            "repo_key": request.get("repo_key"),
            "main_session_id": request.get("main_session_id"),
        },
        "error_or_null": {"type": error_type, "message": message},
        "cleanup_required": False,
    }


def _to_jsonable(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if is_dataclass(value):
        return _to_jsonable(asdict(value), depth + 1)
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item, depth + 1) for key, item in list(value.items())[:80]}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item, depth + 1) for item in list(value)[:80]]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _to_jsonable(model_dump(), depth + 1)
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return _to_jsonable(vars(value), depth + 1)
    return str(value)


def _safe_preview(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    text = " ".join(str(text).split())
    return text[:PREVIEW_LIMIT]


def _safe_report_text(value: Any) -> str:
    if value is None:
        return ""
    limit = _env_int("BRIDGE_OUTER_SDK_REPORT_TEXT_LIMIT") or REPORT_TEXT_LIMIT
    limit = max(PREVIEW_LIMIT, min(limit, 100000))
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    text = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:limit]


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _outer_leader_tools() -> list[str]:
    return _without_outer_forbidden_tools(
        _env_list("OUTER_LEADER_TOOLS", _env_list("OUTER_LEADER_ALLOWED_TOOLS", DEFAULT_ALLOWED_TOOLS))
    )


def _outer_leader_allowed_tools(tools: list[str] | None = None) -> list[str]:
    baseline = tools if tools is not None else _outer_leader_tools()
    return _without_outer_forbidden_tools(_env_list("OUTER_LEADER_ALLOWED_TOOLS", baseline))


def _outer_leader_disallowed_tools() -> list[str]:
    configured = _env_list("OUTER_LEADER_DISALLOWED_TOOLS", DEFAULT_DISALLOWED_TOOLS)
    result: list[str] = []
    for item in [*configured, *sorted(OUTER_LEADER_FORBIDDEN_TOOLS)]:
        if item and item not in result:
            result.append(item)
    return result


def _without_outer_forbidden_tools(tools: list[str]) -> list[str]:
    return [item for item in tools if not _outer_tool_forbidden(item)]


def _outer_tool_forbidden(tool_name: Any) -> bool:
    text = str(tool_name or "").strip()
    if text in OUTER_LEADER_FORBIDDEN_TOOLS:
        return True
    return any(text.startswith(f"{item}(") for item in OUTER_LEADER_FORBIDDEN_TOOLS)


def _has_nonempty(value: Any) -> bool:
    return bool(str(value or "").strip())


def _env_value_ci(env: dict[Any, Any], key: str) -> Any:
    target = key.lower()
    for item_key, value in env.items():
        if str(item_key).lower() == target:
            return value
    return None


_LOOPBACK_NO_PROXY_ENTRIES = ("127.0.0.1", "localhost", "::1")


def _is_loopback_provider_base_url(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        parts = urlsplit(raw)
    except Exception:
        return False
    host = (parts.hostname or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _merge_no_proxy_entries(*values: Any) -> str:
    entries: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in str(value or "").split(","):
            entry = item.strip()
            if not entry:
                continue
            key = entry.lower()
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
    for entry in _LOOPBACK_NO_PROXY_ENTRIES:
        key = entry.lower()
        if key not in seen:
            seen.add(key)
            entries.append(entry)
    return ",".join(entries)


def _ensure_loopback_provider_no_proxy(env: dict[str, str]) -> bool:
    base_url = env.get("ANTHROPIC_BASE_URL") or env.get("CLAUDE_CODE_API_BASE_URL")
    if not _is_loopback_provider_base_url(base_url):
        return False
    merged = _merge_no_proxy_entries(_env_value_ci(env, "NO_PROXY"), _env_value_ci(env, "no_proxy"))
    env["NO_PROXY"] = merged
    env["no_proxy"] = merged
    return True


def _ensure_settings_loopback_provider_no_proxy(settings_payload: dict[str, Any]) -> bool:
    env = settings_payload.get("env")
    if not isinstance(env, dict):
        return False
    base_url = env.get("ANTHROPIC_BASE_URL") or env.get("CLAUDE_CODE_API_BASE_URL")
    if not _is_loopback_provider_base_url(base_url):
        return False
    merged = _merge_no_proxy_entries(_env_value_ci(env, "NO_PROXY"), _env_value_ci(env, "no_proxy"))
    env["NO_PROXY"] = merged
    env["no_proxy"] = merged
    return True


def _no_proxy_includes_loopback(env: dict[Any, Any]) -> bool:
    entries = {
        item.strip().lower()
        for value in (_env_value_ci(env, "NO_PROXY"), _env_value_ci(env, "no_proxy"))
        for item in str(value or "").split(",")
        if item.strip()
    }
    return all(entry.lower() in entries for entry in _LOOPBACK_NO_PROXY_ENTRIES)


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


def _path_exists(value: Any) -> bool:
    if not value:
        return False
    try:
        return Path(str(value)).expanduser().exists()
    except Exception:
        return False


def _safe_command_preview(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parts = shlex.split(raw, posix=(os.name != "nt"))
    except ValueError:
        return _redact_secret_text(raw)
    redacted: list[str] = []
    for part in parts:
        if _is_env_assignment(part):
            key, item_value = part.split("=", 1)
            if _secretish_key(key):
                redacted.append(f"{key}=<redacted>")
            else:
                redacted.append(f"{key}={item_value}")
            continue
        redacted.append(_redact_secret_text(part))
    return " ".join(redacted)


def _secretish_key(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in ("token", "key", "secret", "password", "authorization", "auth"))


def _redact_secret_text(value: str) -> str:
    text = str(value)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1<redacted>", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-<redacted>", text)
    return text


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _sdk_timeout_seconds() -> int | None:
    return _env_int("OUTER_LEADER_SDK_TIMEOUT_SECONDS")


def _outer_claude_query_session_id(request: dict[str, Any]) -> str:
    return _outer_claude_native_session_id(request) or str(request.get("main_session_id") or "default")


def _outer_claude_native_session_id(request: dict[str, Any]) -> str:
    session_id = str(
        request.get("outer_claude_session_id")
        or request.get("outerClaudeSessionId")
        or request.get("claude_session_id")
        or request.get("claudeSessionId")
        or ""
    ).strip()
    if _looks_like_uuid(session_id):
        return session_id
    main_session_id = str(request.get("main_session_id") or "").strip()
    return main_session_id if _looks_like_uuid(main_session_id) else ""


def _looks_like_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value or "").strip())
    except (TypeError, ValueError):
        return False
    return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
