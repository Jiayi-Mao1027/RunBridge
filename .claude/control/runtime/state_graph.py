from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from loader import ControlPaths, load_json_file, load_jsonl
from repo_runtime import infer_repo_key_from_runs_root, repo_key_for_paths
from state_edge import StateEdge
from state_node import StateNode


class RunBridgeState(TypedDict):
    repo_key: str
    run_id: str
    phase: str
    lifecycle_state: str
    graph_node: str
    active_bridge_window_id: str | None
    active_team_id: str | None
    active_task_id: str | None
    frozen_semantics_hash: str | None
    packet_hash: str | None
    completion_contract_hash: str | None
    retry_context: dict[str, Any]
    trajectory_head: dict[str, Any]
    open_process_refs: list[dict[str, Any]]
    pending_approvals: list[dict[str, Any]]
    hard_stops: list[dict[str, Any]]
    diagnostics: list[dict[str, Any]]
    checkpoint_ref: NotRequired[str | None]


TERMINAL_LIFECYCLE_STATES = {
    "bridge_call_denied",
    "bridge_call_failed",
    "bridge_window_returned",
    "bridge_window_partial_returned",
    "bridge_window_failed",
    "bridge_window_orphaned",
    "bridge_window_interrupted",
    "paused_for_user_answer",
    "user_answer_received",
    "resume_same_l3_task",
    "continuation_of_previous_l3",
}


class StateGraph:
    def __init__(
        self,
        *,
        nodes: dict[str, StateNode],
        edges: list[StateEdge],
        raw: dict[str, Any],
    ) -> None:
        self.nodes = nodes
        self.edges = edges
        self.raw = raw
        self._edges_by_event: dict[str, StateEdge] = {}
        self._routes: set[tuple[str, str]] = set()
        for edge in edges:
            for event_kind in edge.event_kinds:
                self._edges_by_event.setdefault(event_kind, edge)
            for route in edge.phase_routes:
                self._routes.add(route)

    @classmethod
    def from_policy(cls, control_root: str | Path) -> "StateGraph":
        policy_path = Path(control_root).expanduser().resolve() / "policy" / "state_graph.json"
        payload = load_json_file(policy_path, default={}) or {}
        nodes: dict[str, StateNode] = {}
        for raw_node in payload.get("nodes", []) if isinstance(payload.get("nodes"), list) else []:
            node = StateNode.from_dict(raw_node)
            nodes[node.node_id] = node
        edges = [StateEdge.from_dict(item) for item in payload.get("edges", []) if isinstance(item, dict)]
        if not nodes:
            raise ValueError(f"state graph policy has no nodes: {policy_path}")
        return cls(nodes=nodes, edges=edges, raw=payload)

    def edge_for_event(self, event_kind: str) -> StateEdge | None:
        return self._edges_by_event.get(str(event_kind))

    def validate_against_policy(self, control_root: str | Path) -> dict[str, Any]:
        root = Path(control_root).expanduser().resolve()
        lifecycle = load_json_file(root / "policy" / "lifecycle_transition_table.json", default={}) or {}
        phase_graph = load_json_file(root / "policy" / "phase_graph.json", default={}) or {}
        lifecycle_events: set[str] = set()
        for item in lifecycle.get("transitions", []) if isinstance(lifecycle.get("transitions"), list) else []:
            if isinstance(item, list) and len(item) == 3:
                lifecycle_events.add(str(item[1]))
        unknown_lifecycle_events = sorted(event for event in lifecycle_events if event not in self._edges_by_event)

        policy_routes: set[tuple[str, str]] = set()
        for phase in phase_graph.get("phases", []) if isinstance(phase_graph.get("phases"), list) else []:
            if not isinstance(phase, dict):
                continue
            source = str(phase.get("name") or "")
            for target in phase.get("allowed_next_phases", []) if isinstance(phase.get("allowed_next_phases"), list) else []:
                policy_routes.add((source, str(target)))
        graph_routes = set(self._routes)
        return {
            "valid": not unknown_lifecycle_events and policy_routes <= graph_routes and graph_routes <= policy_routes,
            "unknown_lifecycle_events": unknown_lifecycle_events,
            "policy_routes_missing_from_state_graph": sorted([list(item) for item in policy_routes - graph_routes]),
            "state_graph_routes_rejected_by_policy": sorted([list(item) for item in graph_routes - policy_routes]),
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
        }

    def replay_events(
        self,
        control_root: str | Path,
        event_records: list[dict[str, Any]],
        *,
        runtime_runs_root: str | Path | None = None,
        repo_key: str | None = None,
        run_id: str | None = None,
    ) -> RunBridgeState:
        transitions = _load_lifecycle_transitions(Path(control_root).expanduser().resolve())
        state = initial_state(
            repo_key=repo_key or infer_repo_key_from_runs_root(runtime_runs_root) or "unscoped_repo",
            run_id=run_id or _first_run_id(event_records),
        )
        window_status: dict[str, str | None] = {}
        for record in event_records:
            apply_event_to_state(state, record, self, transitions, window_status)
        return state

    def export_mermaid(self) -> str:
        lines = ["flowchart TD"]
        for node_id, node in self.nodes.items():
            label = node.description or node_id
            lines.append(f'  {node_id}["{_escape_mermaid(label)}"]')
        for edge in self.edges:
            label_parts = list(edge.event_kinds[:3])
            if len(edge.event_kinds) > 3:
                label_parts.append(f"+{len(edge.event_kinds) - 3} events")
            if edge.phase_routes:
                label_parts.append("phase route")
            label = " / ".join(label_parts)
            if label:
                lines.append(f"  {edge.source} -->|{_escape_mermaid(label)}| {edge.target}")
            else:
                lines.append(f"  {edge.source} --> {edge.target}")
        return "\n".join(lines) + "\n"

    def export_dot(self) -> str:
        lines = ["digraph RunBridgeStateGraph {"]
        for node_id, node in self.nodes.items():
            shape = "doublecircle" if node.terminal else "box"
            lines.append(f'  "{node_id}" [shape={shape}, label="{_escape_dot(node.description or node_id)}"];')
        for edge in self.edges:
            label = ", ".join(edge.event_kinds[:4])
            if len(edge.event_kinds) > 4:
                label += f", +{len(edge.event_kinds) - 4}"
            lines.append(f'  "{edge.source}" -> "{edge.target}" [label="{_escape_dot(label)}"];')
        lines.append("}")
        return "\n".join(lines) + "\n"


def load_state_graph(control_root: str | Path) -> StateGraph:
    return StateGraph.from_policy(control_root)


def validate_state_graph(control_root: str | Path) -> dict[str, Any]:
    return load_state_graph(control_root).validate_against_policy(control_root)


def replay_run_state(control_root: str | Path, run_id: str, *, runtime_runs_root: str | Path | None = None) -> RunBridgeState:
    paths = ControlPaths.from_root(control_root, runtime_runs_root)
    graph = load_state_graph(control_root)
    records = load_jsonl(paths.run_root(run_id) / "event_log.jsonl")
    repo_key = repo_key_for_paths(control_root, runtime_runs_root)
    return graph.replay_events(control_root, records, runtime_runs_root=runtime_runs_root, repo_key=repo_key, run_id=run_id)


def state_from_snapshot(
    snapshot: dict[str, Any],
    *,
    control_root: str | Path,
    runtime_runs_root: str | Path | None = None,
) -> RunBridgeState:
    repo_key = repo_key_for_paths(control_root, runtime_runs_root)
    lifecycle = snapshot.get("lifecycle") if isinstance(snapshot.get("lifecycle"), dict) else {}
    bindings = snapshot.get("bindings") if isinstance(snapshot.get("bindings"), dict) else {}
    open_windows = lifecycle.get("open_bridge_window_ids") if isinstance(lifecycle.get("open_bridge_window_ids"), list) else []
    active_window = str(open_windows[-1]) if open_windows else None
    window_binding = {}
    if active_window:
        bridge_windows = bindings.get("bridge_windows") if isinstance(bindings.get("bridge_windows"), dict) else {}
        window_binding = bridge_windows.get(active_window) if isinstance(bridge_windows.get(active_window), dict) else {}
    integrity = snapshot.get("integrity") if isinstance(snapshot.get("integrity"), dict) else {}
    diagnostics = snapshot.get("runtime_diagnostics") if isinstance(snapshot.get("runtime_diagnostics"), dict) else {}
    semantic = snapshot.get("semantic") if isinstance(snapshot.get("semantic"), dict) else {}
    last_bridge_result = snapshot.get("last_bridge_result") if isinstance(snapshot.get("last_bridge_result"), dict) else {}
    return {
        "repo_key": repo_key,
        "run_id": str(snapshot.get("run_id") or ""),
        "phase": str(snapshot.get("current_phase") or "leader_freeze"),
        "lifecycle_state": str((lifecycle.get("status_index") or {}).get(active_window) or last_bridge_result.get("status") or "read_runtime_truth"),
        "graph_node": "read_runtime_truth",
        "active_bridge_window_id": active_window,
        "active_team_id": window_binding.get("team_id_or_null"),
        "active_task_id": window_binding.get("task_id_or_null"),
        "frozen_semantics_hash": stable_hash(semantic.get("frozen")) if semantic.get("frozen") is not None else None,
        "packet_hash": None,
        "completion_contract_hash": None,
        "retry_context": {},
        "trajectory_head": {},
        "open_process_refs": [],
        "pending_approvals": _list_if_dict_items(integrity.get("pending_approvals") or integrity.get("approval_state")),
        "hard_stops": _list_if_dict_items(integrity.get("hard_stops") or integrity.get("hard_stop")),
        "diagnostics": _diagnostics_list(diagnostics),
    }


def initial_state(*, repo_key: str, run_id: str) -> RunBridgeState:
    return {
        "repo_key": repo_key,
        "run_id": run_id,
        "phase": "leader_freeze",
        "lifecycle_state": "read_runtime_truth",
        "graph_node": "read_runtime_truth",
        "active_bridge_window_id": None,
        "active_team_id": None,
        "active_task_id": None,
        "frozen_semantics_hash": None,
        "packet_hash": None,
        "completion_contract_hash": None,
        "retry_context": {},
        "trajectory_head": {},
        "open_process_refs": [],
        "pending_approvals": [],
        "hard_stops": [],
        "diagnostics": [],
    }


def apply_event_to_state(
    state: RunBridgeState,
    event: dict[str, Any],
    graph: StateGraph,
    lifecycle_transitions: dict[str | None, dict[str, str]],
    window_status: dict[str, str | None],
) -> None:
    event_kind = str(event.get("event_kind") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if event.get("run_id"):
        state["run_id"] = str(event.get("run_id"))
    if event.get("bridge_window_id"):
        state["active_bridge_window_id"] = str(event.get("bridge_window_id"))
    if event.get("team_id"):
        state["active_team_id"] = str(event.get("team_id"))
    if event.get("task_id"):
        state["active_task_id"] = str(event.get("task_id"))
    if event_kind == "semantic_frozen":
        state["frozen_semantics_hash"] = stable_hash(payload.get("frozen_semantics"))
    if event_kind == "phase_advanced" and payload.get("target_phase"):
        state["phase"] = str(payload.get("target_phase"))
    if event_kind == "route_rerouted" and payload.get("target_phase"):
        state["phase"] = str(payload.get("target_phase"))
    packet = payload.get("packet") if isinstance(payload.get("packet"), dict) else None
    if packet:
        state["packet_hash"] = stable_hash(packet)
        completion = packet.get("completion_contract") if isinstance(packet.get("completion_contract"), dict) else None
        if completion is not None:
            state["completion_contract_hash"] = stable_hash(completion)
    if event_kind in {"team_idle_waiting", "wait_timeout_or_process_lost"}:
        refs = payload.get("owned_process_refs") if isinstance(payload.get("owned_process_refs"), list) else []
        state["open_process_refs"] = [ref for ref in refs if isinstance(ref, dict)]
    if event_kind == "retry_attempt_scheduled":
        state["retry_context"] = payload if payload else {k: v for k, v in event.items() if k.startswith("retry_") or k in {"attempt", "max_attempts"}}

    edge = graph.edge_for_event(event_kind)
    if edge:
        state["graph_node"] = edge.target
    elif event_kind:
        state["diagnostics"].append({"level": "warn", "category": "state_graph", "message": "event has no state graph edge", "event_kind": event_kind})

    bridge_window_id = str(event.get("bridge_window_id") or "")
    if bridge_window_id:
        current = window_status.get(bridge_window_id)
        to_status = lifecycle_transitions.get(current, {}).get(event_kind)
        if to_status is None:
            to_status = lifecycle_transitions.get(None, {}).get(event_kind)
        if to_status:
            window_status[bridge_window_id] = to_status
            state["lifecycle_state"] = to_status
            if to_status in TERMINAL_LIFECYCLE_STATES:
                state["active_bridge_window_id"] = None
                state["active_team_id"] = None
                state["active_task_id"] = None


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:24]


def _load_lifecycle_transitions(control_root: Path) -> dict[str | None, dict[str, str]]:
    payload = load_json_file(control_root / "policy" / "lifecycle_transition_table.json", default={}) or {}
    result: dict[str | None, dict[str, str]] = {}
    for item in payload.get("transitions", []) if isinstance(payload.get("transitions"), list) else []:
        if not isinstance(item, list) or len(item) != 3:
            continue
        source_raw, event_kind, target = item
        source = None if source_raw in {None, "null"} else str(source_raw)
        result.setdefault(source, {})[str(event_kind)] = str(target)
    return result


def _first_run_id(records: list[dict[str, Any]]) -> str:
    for record in records:
        if record.get("run_id"):
            return str(record.get("run_id"))
    return ""


def _list_if_dict_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict) and value:
        return [value]
    return []


def _diagnostics_list(value: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key in ("orchestration_anomalies", "execute_watchdog_alerts"):
        items = value.get(key) if isinstance(value.get(key), list) else []
        result.extend(item for item in items if isinstance(item, dict))
    return result


def _escape_mermaid(value: str) -> str:
    return str(value).replace('"', "'").replace("\n", " ")


def _escape_dot(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
