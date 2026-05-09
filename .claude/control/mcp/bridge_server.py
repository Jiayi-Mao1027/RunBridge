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
from repo_runtime import list_registered_repos, list_runs, read_snapshot as read_repo_snapshot  # noqa: E402
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
                    "target_phase": {"type": "string"},
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
        {
            "name": "list_registered_repos",
            "description": "List repo manifests registered under this parent .claude runtime_state registry.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_runs",
            "description": "List run summaries for one registered repo_key.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo_key": {"type": "string"}
                },
                "required": ["repo_key"],
                "additionalProperties": False,
            },
        },
        {
            "name": "read_repo_snapshot",
            "description": "Read a runtime snapshot by explicit repo_key and run_id. Use this for multi-repo-safe snapshot reads.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo_key": {"type": "string"},
                    "run_id": {"type": "string"}
                },
                "required": ["repo_key", "run_id"],
                "additionalProperties": False,
            },
        },
    ]


def _call_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    runtime_runs_root = _effective_runtime_runs_root(arguments)
    if tool_name == "read_runtime_snapshot":
        result = read_runtime_snapshot(CONTROL_ROOT, _resolve_run_id(arguments, runtime_runs_root, require_active=False), runtime_runs_root=runtime_runs_root)
    elif tool_name == "build_bridge_packet":
        arguments = _repair_bridge_packet_arguments(arguments)
        run_id = _resolve_run_id(arguments, runtime_runs_root, require_active=True)
        _freeze_semantics_if_needed(arguments, run_id, runtime_runs_root)
        packet = decide_next_bridge_packet(
            CONTROL_ROOT,
            run_id,
            runtime_runs_root=runtime_runs_root,
            main_session_id=arguments.get("main_session_id"),
            user_instruction=arguments.get("user_instruction"),
            task_spec=arguments.get("task_spec"),
            target_phase=arguments.get("target_phase"),
        )
        _save_last_packet(runtime_runs_root, packet)
        result = {"packet": packet}
    elif tool_name == "call_bridge_sdk":
        packet = arguments.get("packet") if isinstance(arguments.get("packet"), dict) else None
        if packet is None:
            packet = _load_last_packet(runtime_runs_root)
        if packet is None:
            raise ValueError("call_bridge_sdk requires packet; call build_bridge_packet first")
        _resolve_run_id(arguments, runtime_runs_root, require_active=True, packet=packet)
        _ensure_main_bridge_lifecycle_started(CONTROL_ROOT, packet, runtime_runs_root, persist=bool(arguments.get("persist", True)))
        result = {
            "bridge_result": call_bridge_sdk(
                CONTROL_ROOT,
                packet,
                runtime_runs_root=runtime_runs_root,
                persist=bool(arguments.get("persist", True)),
                record_main_lifecycle=False,
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
            _resolve_run_id(arguments, runtime_runs_root, require_active=True),
            runtime_runs_root=runtime_runs_root,
            persist=bool(arguments.get("persist", True)),
        )
    elif tool_name == "list_registered_repos":
        result = {"repos": [repo.as_dict() for repo in list_registered_repos(CONTROL_ROOT)]}
    elif tool_name == "list_runs":
        result = {"repo_key": str(arguments["repo_key"]), "runs": list_runs(CONTROL_ROOT, str(arguments["repo_key"]))}
    elif tool_name == "read_repo_snapshot":
        result = read_repo_snapshot(CONTROL_ROOT, str(arguments["repo_key"]), str(arguments["run_id"]))
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


def _ensure_main_bridge_lifecycle_started(
    control_root: str | Path,
    packet: dict[str, Any],
    runtime_runs_root: str | Path,
    *,
    persist: bool,
) -> None:
    binding = packet.get("binding") if isinstance(packet.get("binding"), dict) else {}
    run_id = str(binding.get("run_id") or "").strip()
    main_session_id = str(binding.get("main_session_id") or run_id).strip()
    sub_session_id = str(binding.get("sub_session_id") or "").strip()
    bridge_window_id = str(binding.get("bridge_window_id") or "").strip()
    if not run_id or not main_session_id or not sub_session_id or not bridge_window_id:
        raise ValueError("packet binding is incomplete")

    snapshot = read_runtime_snapshot(control_root, run_id, runtime_runs_root=runtime_runs_root)
    status = snapshot.get("lifecycle", {}).get("status_index", {}).get(bridge_window_id)
    if status in {"bridge_call_started", "bridge_window_opened", "bridge_packet_accepted", "bridge_packet_rejected"}:
        return
    if status in {
        "team_create_started",
        "team_create_completed",
        "team_create_failed",
        "task_create_started",
        "task_create_completed",
        "task_create_failed",
        "task_created_recorded",
        "message_dispatch_started",
        "message_dispatch_completed",
        "message_dispatch_failed",
        "team_waiting",
        "team_wait_timeout",
        "blocked_for_user_clarification",
        "task_completion_started",
        "task_completion_completed",
        "task_completion_rejected",
        "task_failed",
        "team_delete_started",
        "team_delete_completed",
        "team_delete_failed",
    }:
        return
    if status in {
        "bridge_call_denied",
        "bridge_call_failed",
        "bridge_window_returned",
        "bridge_window_partial_returned",
        "bridge_window_failed",
        "bridge_window_orphaned",
        "paused_for_user_answer",
        "user_answer_received",
        "resume_same_l3_task",
        "continuation_of_previous_l3",
    }:
        raise ValueError(f"bridge window already terminal: {status}")

    base_event = {
        "run_id": run_id,
        "main_session_id": main_session_id,
        "sub_session_id": sub_session_id,
        "bridge_window_id": bridge_window_id,
        "agent_id": binding.get("opened_by_agent_id") or "main-leader",
        "agent_type": "main-leader",
        "tool_name": "mcp__bridge__call_bridge_sdk",
        "tool_use_id": binding.get("parent_tool_use_id"),
        "payload": {"packet": packet},
    }
    needed_by_status = {
        None: ["bridge_call_intended", "pretooluse_allowed_by_main_leader", "call_bridge_sdk_started"],
        "bridge_call_intended": ["pretooluse_allowed_by_main_leader", "call_bridge_sdk_started"],
        "bridge_call_prechecked": ["call_bridge_sdk_started"],
    }
    if status not in needed_by_status:
        raise ValueError(f"bridge window has unexpected lifecycle status before SDK call: {status}")
    for event_kind in needed_by_status[status]:
        result = dispatch_workflow_event(
            control_root,
            {**base_event, "event_kind": event_kind},
            runtime_runs_root=runtime_runs_root,
            persist=persist,
        )
        if not result.ok:
            raise ValueError(f"{event_kind} rejected by runtime: {result.check_result.get('reasons')}")


def _resolve_run_id(
    arguments: dict[str, Any],
    runtime_runs_root: str | Path,
    *,
    require_active: bool,
    packet: dict[str, Any] | None = None,
) -> str:
    run_id = str(arguments.get("run_id") or "").strip()
    if run_id:
        return run_id
    configured = os.environ.get("BRIDGE_RUN_ID")
    if configured and configured.strip():
        return configured.strip()
    if isinstance(packet, dict):
        binding = packet.get("binding") if isinstance(packet.get("binding"), dict) else {}
        packet_run_id = str(binding.get("run_id") or "").strip()
        if packet_run_id:
            return packet_run_id
    active = _load_active_run(runtime_runs_root)
    active_run_id = str(active.get("run_id") or "").strip()
    if active_run_id:
        return active_run_id
    if require_active:
        raise ValueError("active run is required; SessionStart must create .active_run.json before this tool can run")
    return "current"


def _freeze_semantics_if_needed(arguments: dict[str, Any], run_id: str, runtime_runs_root: str | Path) -> None:
    snapshot = read_runtime_snapshot(CONTROL_ROOT, run_id, runtime_runs_root=runtime_runs_root)
    semantic = snapshot.get("semantic", {}) if isinstance(snapshot.get("semantic"), dict) else {}
    if semantic.get("frozen") is not None and not semantic.get("requires_refresh"):
        return
    frozen = _build_frozen_semantics(arguments)
    event = {
        "run_id": run_id,
        "main_session_id": arguments.get("main_session_id") or snapshot.get("main_session_id") or run_id,
        "agent_id": "mcp.build_bridge_packet",
        "agent_type": "main-leader",
        "event_kind": "semantic_frozen",
        "timestamp": _now_iso(),
        "payload": {
            "frozen_semantics": frozen,
            "reason": "build_bridge_packet requires current frozen semantics before bridge dispatch",
        },
    }
    result = dispatch_workflow_event(CONTROL_ROOT, event, runtime_runs_root=runtime_runs_root, persist=True)
    if not result.ok:
        raise ValueError(f"semantic_frozen rejected by runtime: {result.check_result.get('reasons')}")


def _build_frozen_semantics(arguments: dict[str, Any]) -> dict[str, Any]:
    task_spec = arguments.get("task_spec") if isinstance(arguments.get("task_spec"), dict) else {}
    return {
        "user_instruction": _repair_mojibake(arguments.get("user_instruction")),
        "task_subject": _repair_mojibake(task_spec.get("task_subject") or task_spec.get("subject")),
        "task_kind": _repair_mojibake(task_spec.get("task_kind")),
        "target_phase": _repair_mojibake(arguments.get("target_phase")),
        "freeze_source": "mcp_build_bridge_packet",
    }


def _repair_bridge_packet_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    repaired = dict(arguments)
    if "user_instruction" in repaired:
        repaired["user_instruction"] = _repair_mojibake(repaired.get("user_instruction"))
    if "target_phase" in repaired:
        repaired["target_phase"] = _repair_mojibake(repaired.get("target_phase"))
    task_spec = repaired.get("task_spec")
    if isinstance(task_spec, dict):
        repaired["task_spec"] = _repair_mojibake_value(task_spec)
    return repaired


def _repair_mojibake_value(value: Any) -> Any:
    if isinstance(value, str):
        return _repair_mojibake(value)
    if isinstance(value, list):
        return [_repair_mojibake_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _repair_mojibake_value(item) for key, item in value.items()}
    return value


def _repair_mojibake(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    markers = ("锛", "涓", "绯", "娴", "鎵", "閿", "€", "�")
    if not any(marker in value for marker in markers):
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


def _text_quality_score(value: str) -> int:
    mojibake_markers = ("锛", "涓", "绯", "娴", "鎵", "閿", "€", "�", "", "")
    expected_terms = ("系统", "测试", "当前", "项目", "搭建", "框架", "执行", "报错", "失败")
    score = _cjk_score(value)
    score += sum(value.count(term) for term in expected_terms) * 20
    score -= sum(value.count(marker) for marker in mojibake_markers) * 12
    return score


def _cjk_score(value: str) -> int:
    return sum(1 for char in value if "\u4e00" <= char <= "\u9fff") - value.count("�") * 5 - value.count("?") * 2


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


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
