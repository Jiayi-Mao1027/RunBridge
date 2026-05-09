from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loader import load_json_file
from state_graph import stable_hash


STATUS_VALUES = {"succeeded", "failed", "partial", "partial_or_failed"}
COVERAGE_VALUES = {"completed", "deferred", "blocked", "escalated"}


def validation_ok() -> dict[str, Any]:
    return {
        "valid": True,
        "error_type": None,
        "path": None,
        "message": None,
        "repair_allowed": False,
        "retry_allowed": False,
        "next_action": "accept",
    }


def validation_error(
    *,
    error_type: str,
    path: str,
    message: str,
    repair_allowed: bool = True,
    retry_allowed: bool = True,
    next_action: str = "ask_same_bridge_window_to_repair_output",
) -> dict[str, Any]:
    return {
        "valid": False,
        "error_type": error_type,
        "path": path,
        "message": message,
        "repair_allowed": repair_allowed,
        "retry_allowed": retry_allowed,
        "next_action": next_action,
    }


def validate_json_object(value: Any, *, output_name: str = "output") -> dict[str, Any]:
    if isinstance(value, dict):
        return validation_ok()
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            return validation_error(
                error_type="MalformedJson",
                path="$",
                message=f"{output_name} is not valid JSON: {exc}",
                repair_allowed=True,
                retry_allowed=True,
            )
        if isinstance(parsed, dict):
            return validation_ok()
    return validation_error(error_type="InvalidJsonObject", path="$", message=f"{output_name} must be a JSON object")


def validate_bridge_packet(packet: Any, *, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(packet, dict):
        return validation_error(error_type="InvalidBridgePacket", path="$", message="BridgePacket must be an object", retry_allowed=False)
    required = [
        "binding",
        "frozen_semantics",
        "frozen_scope",
        "phase_route",
        "target_phase",
        "team_spec",
        "task_spec",
        "task_team_mapping",
        "completion_contract",
        "report_contract",
        "allowed_tools",
    ]
    for key in required:
        if key not in packet:
            return validation_error(error_type="MissingBridgePacketField", path=f"$.{key}", message=f"BridgePacket missing {key}", retry_allowed=False)
    if not isinstance(packet.get("allowed_tools"), list) or not packet["allowed_tools"]:
        return validation_error(error_type="MissingAllowedTools", path="$.allowed_tools", message="BridgePacket allowed_tools must be non-empty", retry_allowed=False)
    if snapshot:
        semantic = snapshot.get("semantic") if isinstance(snapshot.get("semantic"), dict) else {}
        if semantic.get("frozen") is not None and stable_hash(packet.get("frozen_semantics")) != stable_hash(semantic.get("frozen")):
            return validation_error(
                error_type="FrozenSemanticsMismatch",
                path="$.frozen_semantics",
                message="BridgePacket frozen_semantics hash does not match runtime snapshot",
                repair_allowed=False,
                retry_allowed=False,
                next_action="surface_non_retryable_failure",
            )
    return validation_ok()


def validate_bridge_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return validation_error(error_type="InvalidBridgeResult", path="$", message="bridge result must be an object")
    status = payload.get("status")
    if status not in STATUS_VALUES:
        return validation_error(error_type="InvalidStatus", path="$.status", message="bridge result status is missing or invalid")
    reports = payload.get("reports")
    if not isinstance(reports, list):
        return validation_error(error_type="InvalidReports", path="$.reports", message="bridge result reports must be a list")
    if status in {"succeeded", "partial", "partial_or_failed"} and not reports:
        return validation_error(error_type="MissingReport", path="$.reports", message="non-failed bridge result requires at least one report")
    if not isinstance(payload.get("artifact_refs"), list):
        return validation_error(error_type="InvalidArtifactRefs", path="$.artifact_refs", message="artifact_refs must be a list")
    if "cleanup_required" not in payload or not isinstance(payload.get("cleanup_required"), bool):
        return validation_error(error_type="InvalidCleanupRequired", path="$.cleanup_required", message="cleanup_required must be boolean")
    if status == "succeeded":
        evidence = payload.get("evidence")
        if not isinstance(evidence, dict) or not evidence:
            return validation_error(error_type="MissingRequiredEvidenceRef", path="$.evidence", message="succeeded bridge result requires evidence")
    for index, report in enumerate(reports):
        validation = validate_teammate_report(report, path=f"$.reports[{index}]", strict=False)
        if not validation.get("valid"):
            return validation
    return validation_ok()


def validate_teammate_report(report: Any, *, path: str = "$", strict: bool = True) -> dict[str, Any]:
    if not isinstance(report, dict):
        return validation_error(error_type="InvalidTeammateReport", path=path, message="teammate report must be an object")
    if not report.get("summary"):
        return validation_error(error_type="MissingSummary", path=f"{path}.summary", message="teammate report requires summary")
    if strict:
        coverage = report.get("instruction_coverage")
        if not isinstance(coverage, dict):
            return validation_error(error_type="MissingInstructionCoverage", path=f"{path}.instruction_coverage", message="teammate report requires instruction_coverage")
        for key in coverage:
            if str(key) not in COVERAGE_VALUES:
                return validation_error(error_type="InvalidCoverageDisposition", path=f"{path}.instruction_coverage", message=f"invalid coverage disposition {key}")
    return validation_ok()


def validate_completion_report(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return validation_error(error_type="InvalidCompletionReport", path="$", message="completion report must be an object")
    checks = payload.get("completion_checks") if isinstance(payload.get("completion_checks"), dict) else {}
    if not checks:
        return validation_error(error_type="MissingCompletionChecks", path="$.completion_checks", message="completion report requires completion_checks")
    if checks.get("required_outputs_present") and not payload.get("reports"):
        return validation_error(error_type="MissingReportEvidence", path="$.reports", message="completion output claims reports exist but reports are missing")
    if checks.get("required_artifacts_present") and payload.get("artifact_refs") is None:
        return validation_error(error_type="MissingArtifactRefs", path="$.artifact_refs", message="completion output claims artifacts checked but artifact_refs are absent")
    return validation_ok()


def validate_log_manifest(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return validation_error(error_type="InvalidLogManifest", path="$", message="log manifest must be an object")
    required = _log_manifest_required_fields()
    for field in required:
        if field not in payload or payload.get(field) in {None, ""}:
            return validation_error(error_type="MissingLogManifestField", path=f"$.{field}", message=f"log manifest missing {field}")
    return validation_ok()


def load_schema(control_root: str | Path, schema_name: str) -> dict[str, Any]:
    root = Path(control_root).expanduser().resolve()
    return load_json_file(root / "schemas" / schema_name, default={}) or {}


def _log_manifest_required_fields() -> list[str]:
    return [
        "run_id",
        "bridge_window_id",
        "task_id",
        "command",
        "cwd",
        "environment_evidence",
        "process_refs",
        "terminal_status",
    ]
