from __future__ import annotations

from copy import deepcopy
import json
import os
import re
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
from repo_runtime import get_repo_runtime_root, list_registered_repos, list_runs, read_snapshot as read_repo_snapshot  # noqa: E402
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
                    "repo_key": {"type": "string"},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "build_bridge_packet",
            "description": "Main-leader tool: build exactly one BridgePacket for exactly one bridge invocation window from current runtime truth. If run_id is omitted, use the current project run. This is not terminal for advance/continue requests; after this tool returns, call call_bridge_sdk. If the packet is shown as a bridge_packet-*.txt artifact, call call_bridge_sdk with repo_key and persist=true because the server saved the run-scoped packet.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "repo_key": {"type": "string"},
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
            "description": "Main-leader tool: invoke one bridge session from one BridgePacket. Pass the packet returned by build_bridge_packet; if omitted, the server uses or builds the current run's packet from runtime truth.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo_key": {"type": "string"},
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
                    "repo_key": {"type": "string"},
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
                    "repo_key": {"type": "string"},
                    "persist": {"type": "boolean"},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "mark_bridge_orphaned",
            "description": "Runtime tool: mark one currently blocking bridge window orphaned after runtime evidence shows it has no live execution. If bridge_window_id is omitted and exactly one bridge window blocks phase exit, that window is used.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "repo_key": {"type": "string"},
                    "main_session_id": {"type": "string"},
                    "bridge_window_id": {"type": "string"},
                    "sub_session_id": {"type": "string"},
                    "reason": {"type": "string"},
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
    explicit_arguments = dict(arguments)
    runtime_runs_root = _effective_runtime_runs_root(explicit_arguments)
    arguments = _apply_outer_host_context(explicit_arguments, runtime_runs_root)
    runtime_runs_root = _effective_runtime_runs_root(arguments)
    if tool_name == "read_runtime_snapshot":
        result = read_runtime_snapshot(
            CONTROL_ROOT,
            _resolve_run_id(arguments, runtime_runs_root, require_active=False),
            repo_key=arguments.get("repo_key"),
            runtime_runs_root=runtime_runs_root,
        )
    elif tool_name == "build_bridge_packet":
        arguments = _repair_bridge_packet_arguments(arguments)
        run_id = _resolve_run_id(arguments, runtime_runs_root, require_active=True)
        main_session_id = _resolve_main_session_id(arguments, runtime_runs_root, run_id)
        _freeze_semantics_if_needed(arguments, run_id, runtime_runs_root, main_session_id=main_session_id)
        packet = decide_next_bridge_packet(
            CONTROL_ROOT,
            run_id,
            repo_key=arguments.get("repo_key"),
            runtime_runs_root=runtime_runs_root,
            main_session_id=main_session_id,
            user_instruction=arguments.get("user_instruction"),
            task_spec=arguments.get("task_spec"),
            target_phase=arguments.get("target_phase"),
        )
        _save_last_packet(runtime_runs_root, packet, run_id=run_id)
        result = _packet_built_result(packet, runtime_runs_root, run_id=run_id)
        if _should_auto_dispatch_after_build(arguments, runtime_runs_root, run_id, packet):
            result["auto_dispatched"] = True
            result["auto_dispatch_reason"] = "outer_host_advance_or_continue"
            result["next_required_tool"] = None
            result["next_required_arguments"] = {}
            result["bridge_result"] = _call_bridge_sdk_for_arguments(
                {**arguments, "packet": packet, "persist": arguments.get("persist", True)},
                runtime_runs_root,
                explicit_arguments,
            )
    elif tool_name == "call_bridge_sdk":
        result = {
            "bridge_result": _call_bridge_sdk_for_arguments(arguments, runtime_runs_root, explicit_arguments)
        }
    elif tool_name == "dispatch_workflow_event":
        dispatch_result = dispatch_workflow_event(
            CONTROL_ROOT,
            arguments["event"],
            repo_key=arguments.get("repo_key"),
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
            repo_key=arguments.get("repo_key"),
            runtime_runs_root=runtime_runs_root,
            persist=bool(arguments.get("persist", True)),
        )
    elif tool_name == "mark_bridge_orphaned":
        result = _mark_bridge_orphaned(arguments, runtime_runs_root)
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
    configured_repo_key = os.environ.get("BRIDGE_RUNTIME_REPO_KEY")
    if configured_repo_key and configured_repo_key.strip():
        return str(get_repo_runtime_root(CONTROL_ROOT, configured_repo_key.strip()))
    if os.environ.get("BRIDGE_ALLOW_RUNTIME_RUNS_ROOT_OVERRIDE") == "1" and arguments.get("runtime_runs_root"):
        return str(arguments["runtime_runs_root"])
    default_root = _default_runtime_runs_root()
    context = _load_outer_host_context(default_root)
    context_repo_key = str(context.get("repo_key") or "").strip()
    argument_run_id = _argument_run_id(arguments) or ""
    context_run_id = str(context.get("run_id") or "").strip()
    if context_repo_key and (not argument_run_id or not context_run_id or argument_run_id == context_run_id):
        return str(get_repo_runtime_root(CONTROL_ROOT, context_repo_key))
    explicit_repo_key = _argument_repo_key(arguments)
    if explicit_repo_key:
        return str(get_repo_runtime_root(CONTROL_ROOT, explicit_repo_key))
    return default_root


def _save_last_packet(runtime_runs_root: str | Path, packet: dict[str, Any], *, run_id: str | None = None) -> None:
    path = _last_packet_path(runtime_runs_root, run_id=run_id or _packet_run_id(packet))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(packet, ensure_ascii=True, indent=2), encoding="utf-8")


def _load_last_packet(runtime_runs_root: str | Path, *, run_id: str | None = None) -> dict[str, Any] | None:
    path = _last_packet_path(runtime_runs_root, run_id=run_id)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def _mark_bridge_orphaned(arguments: dict[str, Any], runtime_runs_root: str | Path) -> dict[str, Any]:
    run_id = _resolve_run_id(arguments, runtime_runs_root, require_active=True)
    snapshot = read_runtime_snapshot(
        CONTROL_ROOT,
        run_id,
        repo_key=arguments.get("repo_key"),
        runtime_runs_root=runtime_runs_root,
    )
    bridge_window_id = str(arguments.get("bridge_window_id") or "").strip()
    if not bridge_window_id:
        blocking = snapshot.get("phase_exit_readiness", {}).get("blocking_bridge_window_ids") or []
        if len(blocking) != 1:
            raise ValueError("bridge_window_id is required unless exactly one bridge window blocks phase exit")
        bridge_window_id = str(blocking[0])
    lifecycle = snapshot.get("lifecycle") if isinstance(snapshot.get("lifecycle"), dict) else {}
    status_index = lifecycle.get("status_index") if isinstance(lifecycle.get("status_index"), dict) else {}
    current_status = str(status_index.get(bridge_window_id) or "").strip()
    if current_status in {"bridge_window_returned", "bridge_window_partial_returned", "bridge_window_failed", "bridge_window_orphaned", "bridge_window_interrupted"}:
        return {
            "ok": True,
            "run_id": run_id,
            "bridge_window_id": bridge_window_id,
            "already_terminal": True,
            "status": current_status,
            "runtime_snapshot": snapshot,
        }
    sub_session_id = str(arguments.get("sub_session_id") or "").strip() or _sub_session_id_from_bridge_window_id(bridge_window_id)
    if not sub_session_id:
        raise ValueError("sub_session_id is required when it cannot be derived from bridge_window_id")
    main_session_id = _resolve_main_session_id(arguments, runtime_runs_root, run_id, snapshot)
    reason = str(arguments.get("reason") or "marked orphaned through bridge MCP after runtime evidence showed no live bridge execution").strip()
    event = {
        "run_id": run_id,
        "main_session_id": main_session_id,
        "sub_session_id": sub_session_id,
        "bridge_window_id": bridge_window_id,
        "agent_id": "mcp.mark_bridge_orphaned",
        "agent_type": "runtime",
        "event_kind": "orphan_timeout_without_bridge_return",
        "payload": {
            "reason": reason,
            "last_known_status": current_status or None,
            "last_known_event_ref": current_status or "open_bridge_window",
            "marked_by": "mcp__bridge__mark_bridge_orphaned",
        },
    }
    dispatch_result = dispatch_workflow_event(
        CONTROL_ROOT,
        event,
        repo_key=arguments.get("repo_key"),
        runtime_runs_root=runtime_runs_root,
        persist=bool(arguments.get("persist", True)),
    )
    return {
        "ok": dispatch_result.ok,
        "run_id": dispatch_result.run_id,
        "event_id": dispatch_result.event_id,
        "event_kind": dispatch_result.event_kind,
        "bridge_window_id": bridge_window_id,
        "check_result": dispatch_result.check_result,
        "update_result": dispatch_result.update_result,
        "runtime_snapshot": dispatch_result.runtime_snapshot,
        "written_paths": dispatch_result.written_paths,
    }


def _sub_session_id_from_bridge_window_id(bridge_window_id: str) -> str:
    match = re.search(r"(?:^|_)sub_([A-Za-z0-9]+)$", str(bridge_window_id or ""))
    if not match:
        return ""
    return f"sub_{match.group(1)}"


def _last_packet_path(runtime_runs_root: str | Path, *, run_id: str | None = None) -> Path:
    root = Path(runtime_runs_root)
    if run_id:
        return root / str(run_id) / ".last_bridge_packet.json"
    return root / ".last_bridge_packet.json"


def _packet_run_id(packet: dict[str, Any] | None) -> str | None:
    if not isinstance(packet, dict):
        return None
    binding = packet.get("binding") if isinstance(packet.get("binding"), dict) else {}
    value = str(binding.get("run_id") or packet.get("run_id") or "").strip()
    return value or None


def _packet_main_session_id(packet: dict[str, Any] | None) -> str | None:
    if not isinstance(packet, dict):
        return None
    binding = packet.get("binding") if isinstance(packet.get("binding"), dict) else {}
    value = str(binding.get("main_session_id") or packet.get("main_session_id") or "").strip()
    return value or None


def _packet_repo_key(packet: dict[str, Any]) -> str | None:
    if not isinstance(packet, dict):
        return None
    for source in (packet, packet.get("binding")):
        if isinstance(source, dict):
            value = str(source.get("repo_key") or "").strip()
            if value:
                return value
    return None


def _packet_with_repo_key(packet: dict[str, Any], repo_key: str) -> dict[str, Any]:
    updated = deepcopy(packet)
    binding = updated.setdefault("binding", {})
    if isinstance(binding, dict):
        binding.setdefault("repo_key", repo_key)
    updated.setdefault("repo_key", repo_key)
    return updated


def _packet_built_result(packet: dict[str, Any], runtime_runs_root: str | Path, *, run_id: str) -> dict[str, Any]:
    binding = packet.get("binding") if isinstance(packet.get("binding"), dict) else {}
    contract = packet.get("dispatch_contract") if isinstance(packet.get("dispatch_contract"), dict) else {}
    dispatches = contract.get("teammates") if isinstance(contract.get("teammates"), dict) else {}
    return {
        "schema_version": "bridge_packet_built.v1",
        "packet_saved": True,
        "packet_ref": str(_last_packet_path(runtime_runs_root, run_id=run_id)),
        "repo_key": _packet_repo_key(packet),
        "run_id": run_id,
        "bridge_window_id": binding.get("bridge_window_id"),
        "team_id": binding.get("team_id_or_null"),
        "task_id": binding.get("task_id_or_null"),
        "target_phase": packet.get("target_phase"),
        "dispatch_contract": {
            "schema_version": contract.get("schema_version"),
            "source": contract.get("source"),
            "allowed_agent_subagent_types": list(contract.get("allowed_agent_subagent_types") or []),
            "agent_input_fields_owned_by_system": list(
                (contract.get("agent_call_policy") or {}).get("allowed_input_keys") or []
            ),
            "model_field": (contract.get("agent_call_policy") or {}).get("model_field"),
            "teammate_count": len(dispatches),
        },
        "next_required_tool": "mcp__bridge__call_bridge_sdk",
        "next_required_arguments": {
            "repo_key": _packet_repo_key(packet),
            "run_id": run_id,
            "persist": True,
        },
        "note": "The full BridgePacket is saved run-scoped by the server. Do not reconstruct or edit mechanical packet fields.",
    }


def _argument_packet(arguments: dict[str, Any]) -> dict[str, Any] | None:
    packet = arguments.get("packet")
    return packet if isinstance(packet, dict) else None


def _argument_repo_key(arguments: dict[str, Any]) -> str | None:
    value = str(arguments.get("repo_key") or "").strip()
    if value:
        return value
    return _packet_repo_key(_argument_packet(arguments))


def _argument_run_id(arguments: dict[str, Any]) -> str | None:
    value = str(arguments.get("run_id") or "").strip()
    if value:
        return value
    return _packet_run_id(_argument_packet(arguments))


def _argument_main_session_id(arguments: dict[str, Any]) -> str | None:
    value = str(arguments.get("main_session_id") or "").strip()
    if value:
        return value
    return _packet_main_session_id(_argument_packet(arguments))


def _build_current_run_packet(arguments: dict[str, Any], runtime_runs_root: str | Path, run_id: str) -> dict[str, Any]:
    main_session_id = _resolve_main_session_id(arguments, runtime_runs_root, run_id)
    _freeze_semantics_if_needed(arguments, run_id, runtime_runs_root, main_session_id=main_session_id)
    packet = decide_next_bridge_packet(
        CONTROL_ROOT,
        run_id,
        repo_key=arguments.get("repo_key"),
        runtime_runs_root=runtime_runs_root,
        main_session_id=main_session_id,
        user_instruction=arguments.get("user_instruction"),
        task_spec=arguments.get("task_spec"),
        target_phase=arguments.get("target_phase"),
    )
    _save_last_packet(runtime_runs_root, packet, run_id=run_id)
    return packet


def _call_bridge_sdk_for_arguments(
    arguments: dict[str, Any],
    runtime_runs_root: str | Path,
    explicit_arguments: dict[str, Any],
) -> dict[str, Any]:
    packet = arguments.get("packet") if isinstance(arguments.get("packet"), dict) else None
    packet_repo_key = _packet_repo_key(packet)
    argument_repo_key = str(arguments.get("repo_key") or "").strip()
    if argument_repo_key and packet_repo_key and argument_repo_key != packet_repo_key:
        raise ValueError(f"packet repo_key mismatch: argument={argument_repo_key} packet={packet_repo_key}")
    effective_repo_key = argument_repo_key or packet_repo_key
    if effective_repo_key:
        runtime_runs_root = str(get_repo_runtime_root(CONTROL_ROOT, effective_repo_key))
        if packet is not None and not packet_repo_key:
            packet = _packet_with_repo_key(packet, effective_repo_key)
    resolved_run_id = _resolve_run_id(arguments, runtime_runs_root, require_active=True, packet=packet)
    if packet is None:
        packet = _load_last_packet(runtime_runs_root, run_id=resolved_run_id)
    if packet is None:
        packet = _build_current_run_packet(arguments, runtime_runs_root, resolved_run_id)
    else:
        _save_last_packet(runtime_runs_root, packet, run_id=resolved_run_id)
    _ensure_packet_matches_current_binding(packet, runtime_runs_root, resolved_run_id, explicit_arguments=explicit_arguments)
    _ensure_main_bridge_lifecycle_started(CONTROL_ROOT, packet, runtime_runs_root, persist=bool(arguments.get("persist", True)))
    return call_bridge_sdk(
        CONTROL_ROOT,
        packet,
        runtime_runs_root=runtime_runs_root,
        persist=bool(arguments.get("persist", True)),
        record_main_lifecycle=False,
    )


def _should_auto_dispatch_after_build(
    arguments: dict[str, Any],
    runtime_runs_root: str | Path,
    run_id: str,
    packet: dict[str, Any],
) -> bool:
    context = _load_outer_host_context(runtime_runs_root)
    if str(context.get("run_id") or "").strip() != run_id:
        return False
    intent = str(arguments.get("dispatch_intent") or context.get("dispatch_intent") or "").strip()
    if intent != "advance_or_continue":
        return False
    binding = packet.get("binding") if isinstance(packet.get("binding"), dict) else {}
    required_binding = (
        binding.get("run_id"),
        binding.get("main_session_id"),
        binding.get("sub_session_id"),
        binding.get("bridge_window_id"),
        binding.get("team_id_or_null"),
        binding.get("task_id_or_null"),
    )
    if not all(str(item or "").strip() for item in required_binding):
        return False
    snapshot = read_runtime_snapshot(CONTROL_ROOT, run_id, repo_key=arguments.get("repo_key"), runtime_runs_root=runtime_runs_root)
    if "call_bridge_sdk" not in set(snapshot.get("allowed_actions") or []):
        return False
    integrity = snapshot.get("integrity") if isinstance(snapshot.get("integrity"), dict) else {}
    if any(
        bool(integrity.get(key))
        for key in (
            "has_hard_stop",
            "awaiting_approval",
            "awaiting_user_answer",
            "has_blocking_orchestration_anomaly",
            "has_execute_watchdog_alert",
        )
    ):
        return False
    lifecycle = snapshot.get("lifecycle") if isinstance(snapshot.get("lifecycle"), dict) else {}
    if lifecycle.get("open_bridge_window_ids"):
        return False
    return True


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
    if isinstance(packet, dict):
        packet_run_id = _packet_run_id(packet)
        if packet_run_id:
            return packet_run_id
    context = _load_outer_host_context(runtime_runs_root)
    context_run_id = str(context.get("run_id") or "").strip()
    if context_run_id:
        return context_run_id
    configured = os.environ.get("BRIDGE_RUN_ID")
    if configured and configured.strip():
        return configured.strip()
    active = _load_active_run(runtime_runs_root)
    active_run_id = str(active.get("run_id") or "").strip()
    if active_run_id:
        return active_run_id
    if require_active:
        raise ValueError("active run is required; SessionStart must create .active_run.json before this tool can run")
    return "current"


def _resolve_main_session_id(arguments: dict[str, Any], runtime_runs_root: str | Path, run_id: str, snapshot: dict[str, Any] | None = None) -> str:
    explicit = str(arguments.get("main_session_id") or "").strip()
    if explicit:
        return explicit
    context = _load_outer_host_context(runtime_runs_root)
    if str(context.get("run_id") or "").strip() == run_id:
        context_session = str(context.get("main_session_id") or "").strip()
        if context_session:
            return context_session
    configured = os.environ.get("CLAUDE_CONTROL_MAIN_SESSION_ID") or os.environ.get("BRIDGE_MAIN_SESSION_ID")
    if configured and configured.strip():
        return configured.strip()
    if isinstance(snapshot, dict):
        snapshot_session = str(snapshot.get("main_session_id") or "").strip()
        if snapshot_session:
            return snapshot_session
    return run_id


def _freeze_semantics_if_needed(arguments: dict[str, Any], run_id: str, runtime_runs_root: str | Path, *, main_session_id: str | None = None) -> None:
    snapshot = read_runtime_snapshot(CONTROL_ROOT, run_id, repo_key=arguments.get("repo_key"), runtime_runs_root=runtime_runs_root)
    semantic = snapshot.get("semantic", {}) if isinstance(snapshot.get("semantic"), dict) else {}
    if semantic.get("frozen") is not None and not semantic.get("requires_refresh"):
        return
    resolved_main_session_id = main_session_id or _resolve_main_session_id(arguments, runtime_runs_root, run_id, snapshot)
    frozen = _build_frozen_semantics(arguments)
    event = {
        "run_id": run_id,
        "main_session_id": resolved_main_session_id,
        "agent_id": "mcp.build_bridge_packet",
        "agent_type": "main-leader",
        "event_kind": "semantic_frozen",
        "timestamp": _now_iso(),
        "payload": {
            "repo_key": arguments.get("repo_key"),
            "frozen_semantics": frozen,
            "reason": "build_bridge_packet requires current frozen semantics before bridge dispatch",
        },
    }
    result = dispatch_workflow_event(CONTROL_ROOT, event, repo_key=arguments.get("repo_key"), runtime_runs_root=runtime_runs_root, persist=True)
    if not result.ok:
        raise ValueError(f"semantic_frozen rejected by runtime: {result.check_result.get('reasons')}")


def _ensure_packet_matches_current_binding(
    packet: dict[str, Any],
    runtime_runs_root: str | Path,
    resolved_run_id: str,
    *,
    explicit_arguments: dict[str, Any] | None = None,
) -> None:
    binding = packet.get("binding") if isinstance(packet.get("binding"), dict) else {}
    packet_run_id = str(binding.get("run_id") or packet.get("run_id") or "").strip()
    if packet_run_id and packet_run_id != resolved_run_id:
        raise ValueError(f"packet run_id mismatch: current={resolved_run_id} packet={packet_run_id}")
    context = _load_outer_host_context(runtime_runs_root)
    if not context:
        return
    explicit_arguments = explicit_arguments if isinstance(explicit_arguments, dict) else {}
    explicit_run_id = _argument_run_id(explicit_arguments)
    explicit_main_session_id = _argument_main_session_id(explicit_arguments)
    context_run_id = str(context.get("run_id") or "").strip()
    context_matches_resolved_run = bool(context_run_id and context_run_id == resolved_run_id)
    context_repo_key = str(context.get("repo_key") or "").strip()
    packet_repo_key = _packet_repo_key(packet) or ""
    if context_matches_resolved_run and context_repo_key and packet_repo_key and context_repo_key != packet_repo_key:
        raise ValueError(f"packet repo_key mismatch: outer_host_context={context_repo_key} packet={packet_repo_key}")
    if not explicit_run_id and context_run_id and packet_run_id and context_run_id != packet_run_id:
        raise ValueError(f"packet run_id mismatch: outer_host_context={context_run_id} packet={packet_run_id}")
    context_session = str(context.get("main_session_id") or "").strip()
    packet_session = str(binding.get("main_session_id") or packet.get("main_session_id") or "").strip()
    if context_matches_resolved_run and not explicit_main_session_id and context_session and packet_session and context_session != packet_session:
        raise ValueError(f"packet main_session_id mismatch: outer_host_context={context_session} packet={packet_session}")


def _load_outer_host_context(runtime_runs_root: str | Path) -> dict[str, Any]:
    path = Path(runtime_runs_root).parent / ".outer_host_context.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _apply_outer_host_context(arguments: dict[str, Any], runtime_runs_root: str | Path) -> dict[str, Any]:
    context = _load_outer_host_context(runtime_runs_root)
    if not context:
        return dict(arguments)
    updated = dict(arguments)
    for key in ("repo_key", "run_id"):
        value = context.get(key)
        if value is not None and str(value).strip():
            updated[key] = value
    context_repo_key = str(context.get("repo_key") or "").strip()
    context_run_id = str(context.get("run_id") or "").strip()
    updated_repo_key = _argument_repo_key(updated) or ""
    updated_run_id = _argument_run_id(updated) or ""
    context_matches_repo = not context_repo_key or (updated_repo_key and context_repo_key == updated_repo_key)
    context_matches_run = not context_run_id or (updated_run_id and context_run_id == updated_run_id)
    context_matches_selection = context_matches_repo and context_matches_run
    if context_matches_selection:
        context_session = context.get("main_session_id")
        if context_session is not None and str(context_session).strip():
            updated["main_session_id"] = context_session
    if context_matches_selection:
        for key in ("target_phase", "user_instruction", "dispatch_intent"):
            if str(updated.get(key) or "").strip():
                continue
            value = context.get(key)
            if value is not None and str(value).strip():
                updated[key] = value
        if "task_spec" not in updated or updated.get("task_spec") is None:
            context_task_spec = context.get("task_spec")
            if isinstance(context_task_spec, dict) and context_task_spec:
                updated["task_spec"] = context_task_spec
    return updated


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
