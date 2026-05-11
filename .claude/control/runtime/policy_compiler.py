from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loader import load_json_file
from output_guardrails import validate_schema


@dataclass(frozen=True, slots=True)
class CompiledPolicy:
    schema_version: str
    phase_contracts: dict[str, Any]
    approval_matrix: dict[str, Any]
    reconcile_rules: dict[str, Any]
    lifecycle_transition_table: dict[str, Any]
    phase_graph: dict[str, Any]
    schemas: dict[str, dict[str, Any]]
    team_planner: dict[str, Any]
    validation_results: list[dict[str, Any]]
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
        approval_matrix = _dict(load_json_file(policy_root / "approval_matrix.json", default={}))
        reconcile_rules = _dict(load_json_file(policy_root / "reconcile_rules.json", default={}))
        lifecycle_transition_table = _dict(load_json_file(policy_root / "lifecycle_transition_table.json", default={}))
        phase_graph = _dict(load_json_file(policy_root / "phase_graph.json", default={}))
        validation_results = _validate_policy_sources(self.control_root, phase_contracts)
        return CompiledPolicy(
            schema_version=str(phase_contracts.get("schema_version") or "unknown"),
            phase_contracts=phase_contracts,
            approval_matrix=approval_matrix,
            reconcile_rules=reconcile_rules,
            lifecycle_transition_table=lifecycle_transition_table,
            phase_graph=phase_graph,
            schemas=schemas,
            team_planner=_dict(phase_contracts.get("team_planner")),
            validation_results=validation_results,
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


def _validate_policy_sources(control_root: Path, phase_contracts: dict[str, Any]) -> list[dict[str, Any]]:
    checks = [
        {
            "source": "control/policy/phase_contracts.json",
            **validate_schema(phase_contracts, "phase_contracts.schema.json", control_root=control_root),
        }
    ]
    phases = phase_contracts.get("phases") if isinstance(phase_contracts.get("phases"), dict) else {}
    team_planner = phase_contracts.get("team_planner") if isinstance(phase_contracts.get("team_planner"), dict) else {}
    checks.append(_validate_team_planner_references(phases, team_planner))
    return checks


def _validate_team_planner_references(phases: dict[str, Any], team_planner: dict[str, Any]) -> dict[str, Any]:
    names_by_phase: dict[str, set[str]] = {}
    for phase, config in phases.items():
        if not isinstance(config, dict):
            continue
        names_by_phase[str(phase)] = {
            str(item.get("teammate_name"))
            for item in config.get("teammates", [])
            if isinstance(item, dict) and item.get("teammate_name")
        }
    phase_rules = team_planner.get("phase_rules") if isinstance(team_planner.get("phase_rules"), dict) else {}
    missing: list[dict[str, str]] = []
    for phase, rule in phase_rules.items():
        if not isinstance(rule, dict):
            continue
        selected = str(rule.get("selected_teammate") or "")
        if selected and selected not in names_by_phase.get(str(phase), set()):
            missing.append({"phase": str(phase), "selected_teammate": selected})
    return {
        "valid": not missing,
        "error_type": None if not missing else "InvalidTeamPlannerPolicy",
        "path": None if not missing else "$.team_planner.phase_rules",
        "message": None if not missing else "team planner phase rule selects teammate absent from phase policy",
        "repair_allowed": bool(missing),
        "retry_allowed": False,
        "next_action": "accept" if not missing else "repair_policy_source",
        "source": "control/policy/phase_contracts.json#team_planner",
        "missing_teammates": missing,
    }
