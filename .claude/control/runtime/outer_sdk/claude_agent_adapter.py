from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import threading
from typing import Any

from .adapters import OuterLeaderEventSink


SDK_PACKAGE = "claude_agent_sdk"
DEFAULT_ALLOWED_TOOLS = [
    "mcp__bridge__read_runtime_snapshot",
    "mcp__bridge__build_bridge_packet",
    "mcp__bridge__call_bridge_sdk",
    "mcp__bridge__reconcile_workflow_from_ledger",
    "Read",
    "Grep",
    "Glob",
    "LS",
]
DEFAULT_DISALLOWED_TOOLS = ["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"]
PREVIEW_LIMIT = 700


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
        self._emit(
            event_sink,
            request,
            "sdk_stream_started",
            {"session_id": request.get("main_session_id"), "input_id": request.get("input_id")},
        )
        try:
            await client.query(prompt, session_id=str(request.get("main_session_id") or "default"))
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
            {"session_id": request.get("main_session_id"), "message_count": len(messages)},
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
        system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": leader_prompt,
            "exclude_dynamic_sections": True,
        }
        return _construct_options(
            options_cls,
            {
                "system_prompt": system_prompt,
                "cwd": str(repo_root),
                "mcp_servers": _bridge_mcp_servers(control_root),
                "strict_mcp_config": True,
                "allowed_tools": _env_list("OUTER_LEADER_ALLOWED_TOOLS", DEFAULT_ALLOWED_TOOLS),
                "disallowed_tools": _env_list("OUTER_LEADER_DISALLOWED_TOOLS", DEFAULT_DISALLOWED_TOOLS),
                "setting_sources": [],
                "include_partial_messages": True,
                "max_turns": _env_int("OUTER_LEADER_MAX_TURNS"),
                "max_budget_usd": _env_float("OUTER_LEADER_MAX_BUDGET_USD"),
                "env": {
                    "CLAUDE_CONTROL_ROOT": str(control_root),
                    "BRIDGE_RUNTIME_REPO_KEY": str(request.get("repo_key") or ""),
                    "BRIDGE_RUN_ID": str(request.get("run_id") or ""),
                    "CLAUDE_CONTROL_RUN_ID": str(request.get("run_id") or ""),
                    "PYTHONNOUSERSITE": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            },
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
    return {
        "bridge": {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(control_root / "mcp" / "bridge_server.py")],
            "env": {
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "CLAUDE_CONTROL_ROOT": str(control_root),
            },
        }
    }


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


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    marker = text.find("\n---", 3)
    if marker == -1:
        return text
    return text[marker + 4 :].lstrip()


def _build_user_prompt(request: dict[str, Any]) -> str:
    metadata = {
        "repo_key": request.get("repo_key"),
        "run_id": request.get("run_id"),
        "main_session_id": request.get("main_session_id"),
        "input_kind": request.get("input_kind"),
        "target_phase": request.get("target_phase"),
        "input_id": request.get("input_id"),
    }
    return (
        "RunBridge outer host user input.\n"
        "Use runtime truth and bridge MCP tools; do not treat this wrapper metadata as project evidence.\n\n"
        f"Metadata:\n{json.dumps(metadata, ensure_ascii=False, indent=2)}\n\n"
        f"User message:\n{request.get('text') or ''}"
    )


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
    for key in ("subtype", "result", "session_id", "total_cost_usd", "duration_ms", "num_turns"):
        if key in payload:
            value = payload.get(key)
            fields[key] = _safe_preview(value) if key == "result" else value
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
    status = "succeeded" if subtype in {None, "success"} else "failed"
    summary = _last_preview(messages) or "outer leader SDK response completed"
    return {
        "status": status,
        "handled_by": handled_by,
        "reports": [
            {
                "summary": summary,
                "source": "outer_leader_sdk",
                "session_id": request.get("main_session_id"),
                "message_count": len(messages),
            }
        ],
        "artifact_refs": [],
        "evidence": {
            "outer_sdk_session_id": request.get("main_session_id"),
            "sdk_message_count": len(messages),
            "result_subtype": subtype,
            "runtime_event_id": request.get("runtime_event_id"),
        },
        "error_or_null": None
        if status == "succeeded"
        else {
            "type": "OuterLeaderSdkResultNotSuccess",
            "message": str(subtype or "SDK response ended without a success result"),
        },
        "cleanup_required": False,
    }


def _last_preview(messages: list[dict[str, Any]]) -> str | None:
    for item in reversed(messages):
        preview = item.get("message_preview")
        if preview:
            return str(preview)[:PREVIEW_LIMIT]
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


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


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


def _sdk_timeout_seconds() -> int:
    return _env_int("OUTER_LEADER_SDK_TIMEOUT_SECONDS") or 600


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
