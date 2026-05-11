from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loader import load_json_file


@dataclass(frozen=True, slots=True)
class CompiledPolicy:
    schema_version: str
    phase_contracts: dict[str, Any]
    approval_matrix: dict[str, Any]
    reconcile_rules: dict[str, Any]
    lifecycle_transition_table: dict[str, Any]
    phase_graph: dict[str, Any]
    schemas: dict[str, dict[str, Any]]
    source_refs: dict[str, str]

    def phase_config(self, phase: str) -> dict[str, Any]:
        phases = self.phase_contracts.get("phases")
        config = phases.get(phase) if isinstance(phases, dict) else None
        return config if isinstance(config, dict) else {}


class PolicyCompiler:
    """Compile policy JSON and schemas into one runtime-facing policy object."""

    def __init__(self, control_root: str | Path) -> None:
        self.control_root = Path(control_root).expanduser().resolve()

    def compile(self) -> CompiledPolicy:
        policy_root = self.control_root / "policy"
        schema_root = self.control_root / "schemas"
        schemas: dict[str, dict[str, Any]] = {}
        if schema_root.exists():
            for path in sorted(schema_root.glob("*.schema.json")):
                payload = load_json_file(path, default={}) or {}
                if isinstance(payload, dict):
                    schemas[path.name] = payload
        phase_contracts = _dict(load_json_file(policy_root / "phase_contracts.json", default={}))
        return CompiledPolicy(
            schema_version=str(phase_contracts.get("schema_version") or "unknown"),
            phase_contracts=phase_contracts,
            approval_matrix=_dict(load_json_file(policy_root / "approval_matrix.json", default={})),
            reconcile_rules=_dict(load_json_file(policy_root / "reconcile_rules.json", default={})),
            lifecycle_transition_table=_dict(load_json_file(policy_root / "lifecycle_transition_table.json", default={})),
            phase_graph=_dict(load_json_file(policy_root / "phase_graph.json", default={})),
            schemas=schemas,
            source_refs={
                "phase_contracts": str(policy_root / "phase_contracts.json"),
                "approval_matrix": str(policy_root / "approval_matrix.json"),
                "reconcile_rules": str(policy_root / "reconcile_rules.json"),
                "lifecycle_transition_table": str(policy_root / "lifecycle_transition_table.json"),
                "phase_graph": str(policy_root / "phase_graph.json"),
                "schemas": str(schema_root),
            },
        )


def compile_policy(control_root: str | Path) -> CompiledPolicy:
    return PolicyCompiler(control_root).compile()


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}

