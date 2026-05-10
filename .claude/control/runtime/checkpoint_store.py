from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loader import ControlPaths
from persist import append_jsonl, atomic_write_json
from state_graph import RunBridgeState, replay_run_state, stable_hash, state_from_snapshot


class CheckpointStore:
    """Durable per-event RunBridge state checkpoint store."""

    def __init__(self, paths: ControlPaths, run_id: str) -> None:
        self.paths = paths
        self.run_id = run_id
        self.run_root = paths.run_root(run_id)
        self.root = self.run_root / "checkpoints"

    def write_checkpoint(
        self,
        *,
        state: RunBridgeState,
        event: Any,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        event_id = str(getattr(event, "event_id", None) or _event_value(event, "event_id") or "event")
        event_kind = str(getattr(event, "event_kind", None) or _event_value(event, "event_kind") or "unknown")
        sequence = _next_sequence(self.run_root / "checkpoints.jsonl")
        payload = {
            "schema_version": "0.1.0",
            "checkpoint_id": f"chkpt_{sequence:06d}_{_safe_component(event_id)}",
            "sequence": sequence,
            "timestamp": _now_iso(),
            "repo_key": state.get("repo_key"),
            "run_id": self.run_id,
            "event_id": event_id,
            "event_kind": event_kind,
            "state": state,
            "state_hash": stable_hash(state),
            "snapshot_hash": stable_hash(snapshot),
        }
        path = self.root / f"{sequence:06d}_{_safe_component(event_id)}.json"
        atomic_write_json(path, payload)
        index_record = {
            "checkpoint_id": payload["checkpoint_id"],
            "sequence": sequence,
            "timestamp": payload["timestamp"],
            "repo_key": state.get("repo_key"),
            "run_id": self.run_id,
            "event_id": event_id,
            "event_kind": event_kind,
            "state_hash": payload["state_hash"],
            "path": str(path),
        }
        append_jsonl(self.run_root / "checkpoints.jsonl", index_record)
        atomic_write_json(self.run_root / "latest_checkpoint.json", {**index_record, "state": state})
        return index_record

    def latest(self) -> dict[str, Any]:
        path = self.run_root / "latest_checkpoint.json"
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}


def write_event_checkpoint(paths: ControlPaths, event: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    run_id = str(getattr(event, "run_id", None) or _event_value(event, "run_id") or snapshot.get("run_id") or "")
    if not run_id:
        return {}
    try:
        state = replay_run_state(paths.control_root, run_id, runtime_runs_root=paths.runtime_runs_root)
    except Exception:
        state = state_from_snapshot(snapshot, control_root=paths.control_root, runtime_runs_root=paths.runtime_runs_root)
    store = CheckpointStore(paths, run_id)
    return store.write_checkpoint(state=state, event=event, snapshot=snapshot)


def _event_value(event: Any, key: str) -> Any:
    if isinstance(event, dict):
        return event.get(key)
    return None


def _next_sequence(path: Path) -> int:
    if not path.exists():
        return 1
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) + 1
    except Exception:
        return 1


def _safe_component(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(value))[:80] or "event"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
