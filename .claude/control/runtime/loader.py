from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from models import LoadedState


@dataclass(slots=True)
class ControlPaths:
    control_root: Path
    runtime_runs_root: Path

    @classmethod
    def from_root(
        cls,
        control_root: str | Path,
        runtime_runs_root: str | Path | None = None,
    ) -> "ControlPaths":
        control_root_path = Path(control_root).expanduser().resolve()
        runtime_root = (
            Path(runtime_runs_root).expanduser().resolve()
            if runtime_runs_root is not None
            else control_root_path / "runtime_state" / "runs"
        )
        return cls(control_root=control_root_path, runtime_runs_root=runtime_root)

    def run_root(self, run_id: str) -> Path:
        return self.runtime_runs_root / run_id

    def run_ledger_path(self, run_id: str) -> Path:
        return self.run_root(run_id) / "run_ledger.json"

    def tasks_root(self, run_id: str) -> Path:
        return self.run_root(run_id) / "tasks"

    def transitions_path(self, run_id: str) -> Path:
        return self.run_root(run_id) / "transitions.jsonl"

    def phase_graph_path(self) -> Path:
        return self.control_root / "policy" / "phase_graph.json"

    def approval_matrix_path(self) -> Path:
        return self.control_root / "policy" / "approval_matrix.json"

    def reconcile_rules_path(self) -> Path:
        return self.control_root / "policy" / "reconcile_rules.json"


def load_json_file(path: Path, *, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def load_run_ledger(paths: ControlPaths, run_id: str) -> dict[str, Any]:
    run_ledger = load_json_file(paths.run_ledger_path(run_id), default=None)
    if run_ledger is None:
        raise FileNotFoundError(f"Missing run ledger for run_id={run_id}: {paths.run_ledger_path(run_id)}")
    return run_ledger


def load_task_ledgers(paths: ControlPaths, run_id: str) -> dict[str, dict[str, Any]]:
    root = paths.tasks_root(run_id)
    if not root.exists():
        return {}
    task_ledgers: dict[str, dict[str, Any]] = {}
    for file_path in sorted(root.glob('*.json')):
        payload = load_json_file(file_path, default={})
        if not isinstance(payload, dict):
            continue
        task_id = str(payload.get('task_id') or file_path.stem)
        task_ledgers[task_id] = payload
    return task_ledgers


def load_transition_records(paths: ControlPaths, run_id: str) -> list[dict[str, Any]]:
    return load_jsonl(paths.transitions_path(run_id))


def load_state(
    control_root: str | Path,
    run_id: str,
    *,
    runtime_runs_root: str | Path | None = None,
) -> LoadedState:
    paths = ControlPaths.from_root(control_root, runtime_runs_root)
    return LoadedState(
        control_root=str(paths.control_root),
        runtime_runs_root=str(paths.runtime_runs_root),
        run_ledger=load_run_ledger(paths, run_id),
        task_ledgers=load_task_ledgers(paths, run_id),
        transition_records=load_transition_records(paths, run_id),
        phase_graph=load_json_file(paths.phase_graph_path(), default={}) or {},
        approval_matrix=load_json_file(paths.approval_matrix_path(), default={}) or {},
        reconcile_rules=load_json_file(paths.reconcile_rules_path(), default={}) or {},
    )
