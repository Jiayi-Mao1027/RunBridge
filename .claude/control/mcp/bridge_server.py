from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any


SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2025-03-26"

SERVER_ROOT = Path(__file__).resolve().parent
CONTROL_ROOT = SERVER_ROOT.parent
WORKFLOW_ROOT = CONTROL_ROOT.parent
PROJECT_ROOT = Path(os.environ.get("BRIDGE_PROJECT_ROOT") or Path.cwd()).resolve()
RUNTIME_ROOT = CONTROL_ROOT / "runtime"
USE_FRAMED_STDIO = False

sys.path.insert(0, str(RUNTIME_ROOT))

from bridge_sdk import call_bridge_sdk  # noqa: E402
from main_leader import decide_next_bridge_packet, read_runtime_snapshot  # noqa: E402
from workflow_runtime import dispatch_workflow_event, reconcile_workflow_from_ledger  # noqa: E402


def main() -> None:
    global USE_FRAMED_STDIO
    first = sys.stdin.buffer.peek(1)[:1]
    if first and first not in {b" ", b"\t", b"\r", b"\n", b"{", b"["}:
        USE_FRAMED_STDIO = True
        _run_framed_stdio()
        return
    _run_line_stdio()


def _run_line_stdio() -> None:
    while True:
        raw_line = sys.stdin.buffer.readline()
        if raw_line == b"":
            return
        line = raw_line.decode("utf-8").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            if isinstance(message, list):
                responses = [_handle_message(item) for item in message]
                responses = [item for item in responses if item is not None]
                if responses:
                    _send(responses)
            else:
                response = _handle_message(message)
                if response is not None:
                    _send(response)
        except Exception as exc:
            _send(_error_response(None, -32700, f"invalid MCP message: {exc}"))


def _run_framed_stdio() -> None:
    while True:
        try:
            message = _read_framed_message()
        except EOFError:
            return
        except Exception as exc:
            _send(_error_response(None, -32700, f"invalid MCP message: {exc}"))
            continue

        if isinstance(message, list):
            responses = [_handle_message(item) for item in message]
            responses = [item for item in responses if item is not None]
            if responses:
                _send(responses)
        else:
            response = _handle_message(message)
            if response is not None:
                _send(response)


def _read_framed_message() -> Any:
    headers: dict[str, str] = {}
    while True:
        raw = sys.stdin.buffer.readline()
        if raw == b"":
            raise EOFError
        line = raw.decode("ascii", errors="replace").strip()
        if not line:
            break
        key, sep, value = line.partition(":")
        if sep:
            headers[key.lower()] = value.strip()

    content_length = headers.get("content-length")
    if not content_length:
        raise ValueError("missing Content-Length header")
    body = sys.stdin.buffer.read(int(content_length))
    if not body:
        raise EOFError
    return json.loads(body.decode("utf-8"))


def _handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return _error_response(None, -32600, "JSON-RPC message must be an object")

    request_id = message.get("id")
    method = str(message.get("method") or "")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if request_id is None:
        _handle_notification(method, params)
        return None

    try:
        if method == "initialize":
            return _result_response(request_id, _initialize_result())
        if method == "ping":
            return _result_response(request_id, {})
        if method == "tools/list":
            return _result_response(request_id, {"tools": _tools()})
        if method == "tools/call":
            tool_name = str(params.get("name") or "")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            return _result_response(request_id, _call_tool(tool_name, arguments))
        if method == "resources/list":
            return _result_response(request_id, {"resources": []})
        if method == "prompts/list":
            return _result_response(request_id, {"prompts": []})
        return _error_response(request_id, -32601, f"unsupported method: {method}")
    except Exception as exc:
        print(traceback.format_exc(), file=sys.stderr, flush=True)
        return _error_response(request_id, -32000, str(exc))


def _handle_notification(method: str, params: dict[str, Any]) -> None:
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return
    print(f"bridge MCP ignored notification {method}: {params}", file=sys.stderr, flush=True)


def _initialize_result() -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {},
            "prompts": {},
        },
        "serverInfo": {
            "name": "bridge",
            "version": SERVER_VERSION,
        },
    }


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "read_runtime_snapshot",
            "description": "Read the authoritative bridge workflow RuntimeSnapshot for a run. If run_id is omitted, use the current project run.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "build_bridge_packet",
            "description": "Main-leader tool: build exactly one BridgePacket for exactly one bridge invocation window from current runtime truth. If run_id is omitted, use the current project run.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "main_session_id": {"type": "string"},
                    "user_instruction": {"type": "string"},
                    "task_spec": {"type": "object"},
                    "team_spec": {"type": "object"},
                    "completion_contract": {"type": "object"},
                    "report_contract": {"type": "object"},
                    "target_phase": {"type": "string"},
                    "phase_route": {"type": "array", "items": {"type": "string"}},
                    "approval_requirements": {"type": "array", "items": {"type": "object"}},
                    "expires_in_seconds": {"type": "integer"},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "call_bridge_sdk",
            "description": "Main-leader tool: invoke one bridge session from one BridgePacket. Pass the packet returned by build_bridge_packet; if omitted, the server uses the most recent packet built for this project.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet": {"type": "object"},
                    "persist": {"type": "boolean"},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "dispatch_workflow_event",
            "description": "Runtime tool: dispatch one explicit RuntimeEvent through check/update/notify/persist.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "event": {"type": "object"},
                    "persist": {"type": "boolean"},
                },
                "required": ["event"],
                "additionalProperties": False,
            },
        },
        {
            "name": "reconcile_workflow_from_ledger",
            "description": "Replay event_log.jsonl for a run and rebuild runtime snapshot, transitions, and run ledger. If run_id is omitted, use the current project run.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "persist": {"type": "boolean"},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    ]


def _call_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    runtime_runs_root = _effective_runtime_runs_root(arguments)
    if tool_name == "read_runtime_snapshot":
        result = read_runtime_snapshot(CONTROL_ROOT, _resolve_run_id(arguments, runtime_runs_root), runtime_runs_root=runtime_runs_root)
    elif tool_name == "build_bridge_packet":
        packet = decide_next_bridge_packet(
            CONTROL_ROOT,
            _resolve_run_id(arguments, runtime_runs_root),
            runtime_runs_root=runtime_runs_root,
            main_session_id=arguments.get("main_session_id"),
            user_instruction=arguments.get("user_instruction"),
            task_spec=arguments.get("task_spec"),
            team_spec=arguments.get("team_spec"),
            completion_contract=arguments.get("completion_contract"),
            report_contract=arguments.get("report_contract"),
            target_phase=arguments.get("target_phase"),
            phase_route=arguments.get("phase_route"),
            approval_requirements=arguments.get("approval_requirements"),
            expires_in_seconds=arguments.get("expires_in_seconds"),
        )
        _save_last_packet(runtime_runs_root, packet)
        result = {"packet": packet}
    elif tool_name == "call_bridge_sdk":
        packet = arguments.get("packet") if isinstance(arguments.get("packet"), dict) else None
        used_cached_packet = packet is None
        if packet is None:
            packet = _load_last_packet(runtime_runs_root)
        if packet is None:
            raise ValueError("call_bridge_sdk requires packet; call build_bridge_packet first")
        result = {
            "bridge_result": call_bridge_sdk(
                CONTROL_ROOT,
                packet,
                runtime_runs_root=runtime_runs_root,
                persist=bool(arguments.get("persist", True)),
                record_main_lifecycle=used_cached_packet,
            )
        }
    elif tool_name == "dispatch_workflow_event":
        dispatch_result = dispatch_workflow_event(
            CONTROL_ROOT,
            arguments["event"],
            runtime_runs_root=runtime_runs_root,
            persist=bool(arguments.get("persist", True)),
        )
        result = {
            "ok": dispatch_result.ok,
            "run_id": dispatch_result.run_id,
            "event_id": dispatch_result.event_id,
            "event_kind": dispatch_result.event_kind,
            "check_result": dispatch_result.check_result,
            "update_result": dispatch_result.update_result,
            "notify_result": dispatch_result.notify_result,
            "runtime_snapshot": dispatch_result.runtime_snapshot,
            "written_paths": dispatch_result.written_paths,
        }
    elif tool_name == "reconcile_workflow_from_ledger":
        result = reconcile_workflow_from_ledger(
            CONTROL_ROOT,
            _resolve_run_id(arguments, runtime_runs_root),
            runtime_runs_root=runtime_runs_root,
            persist=bool(arguments.get("persist", True)),
        )
    else:
        raise ValueError(f"unknown tool: {tool_name}")

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False, indent=2),
            }
        ],
        "isError": False,
    }


def _default_runtime_runs_root() -> str:
    configured = os.environ.get("BRIDGE_RUNTIME_RUNS_ROOT")
    if configured:
        return configured
    project_key = _project_state_key(PROJECT_ROOT)
    return str(WORKFLOW_ROOT / "runtime_state" / "projects" / project_key / "runs")


def _effective_runtime_runs_root(arguments: dict[str, Any]) -> str:
    if os.environ.get("BRIDGE_ALLOW_RUNTIME_RUNS_ROOT_OVERRIDE") == "1" and arguments.get("runtime_runs_root"):
        return str(arguments["runtime_runs_root"])
    return _default_runtime_runs_root()


def _save_last_packet(runtime_runs_root: str | Path, packet: dict[str, Any]) -> None:
    path = _last_packet_path(runtime_runs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(packet, ensure_ascii=True, indent=2), encoding="utf-8")


def _load_last_packet(runtime_runs_root: str | Path) -> dict[str, Any] | None:
    path = _last_packet_path(runtime_runs_root)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def _last_packet_path(runtime_runs_root: str | Path) -> Path:
    return Path(runtime_runs_root) / ".last_bridge_packet.json"


def _resolve_run_id(arguments: dict[str, Any], runtime_runs_root: str | Path) -> str:
    run_id = str(arguments.get("run_id") or "").strip()
    if run_id:
        return run_id
    configured = os.environ.get("BRIDGE_RUN_ID")
    if configured and configured.strip():
        return configured.strip()
    active = _load_active_run(runtime_runs_root)
    active_run_id = str(active.get("run_id") or "").strip()
    if active_run_id:
        return active_run_id
    return "current"


def _load_active_run(runtime_runs_root: str | Path) -> dict[str, Any]:
    path = Path(runtime_runs_root) / ".active_run.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _project_state_key(project_root: Path) -> str:
    import hashlib

    normalized = str(project_root).lower()
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{project_root.name}_{digest}"


def _result_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_response(request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _send(message: dict[str, Any] | list[dict[str, Any]]) -> None:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if USE_FRAMED_STDIO:
        sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        return
    sys.stdout.buffer.write(payload + b"\n")
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
