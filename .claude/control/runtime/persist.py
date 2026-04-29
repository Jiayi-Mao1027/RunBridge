from __future__ import annotations

import json
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from loader import ControlPaths
from models import DispatchResult


def sanitize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, dict):
        return {sanitize_json_value(key): sanitize_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_json_value(item) for item in value]
    return value


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as tmp:
        json.dump(sanitize_json_value(payload), tmp, ensure_ascii=False, indent=2, sort_keys=False)
        tmp.flush()
        temp_path = Path(tmp.name)
    _replace_with_retry(temp_path, path)


def _replace_with_retry(temp_path: Path, path: Path) -> None:
    delays = [0.02, 0.05, 0.1, 0.2, 0.4]
    for delay in delays:
        try:
            temp_path.replace(path)
            return
        except PermissionError:
            time.sleep(delay)
    temp_path.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(sanitize_json_value(payload), ensure_ascii=False)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def persist_dispatch_result(
    control_root: str | Path,
    dispatch_result: DispatchResult,
    *,
    runtime_runs_root: str | Path | None = None,
) -> dict[str, str]:
    paths = ControlPaths.from_root(control_root, runtime_runs_root)
    run_id = dispatch_result.run_id

    written: dict[str, str] = {}

    transition_path = paths.transitions_path(run_id)
    append_jsonl(transition_path, dispatch_result.transition_record)
    written["transition_record"] = str(transition_path)

    tasks_root = paths.tasks_root(run_id)
    for task_id, payload in dispatch_result.task_ledgers.items():
        task_path = tasks_root / f"{task_id}.json"
        atomic_write_json(task_path, payload)
    written["tasks_root"] = str(tasks_root)

    reconcile_path = paths.run_root(run_id) / "reconcile_result.json"
    atomic_write_json(reconcile_path, dispatch_result.reconcile_result)
    written["reconcile_result"] = str(reconcile_path)

    run_ledger_path = paths.run_ledger_path(run_id)
    atomic_write_json(run_ledger_path, dispatch_result.run_ledger)
    written["run_ledger"] = str(run_ledger_path)

    return written
