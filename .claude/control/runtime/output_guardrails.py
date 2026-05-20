from __future__ import annotations

import json
from pathlib import Path
import re
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


def validate_bridge_packet(packet: Any, *, snapshot: dict[str, Any] | None = None, control_root: str | Path | None = None) -> dict[str, Any]:
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


def validate_bridge_result(
    payload: Any,
    *,
    control_root: str | Path | None = None,
    completion_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return validation_error(error_type="InvalidBridgeResult", path="$", message="bridge result must be an object")
    schema_validation = validate_schema(payload, "bridge_result.schema.json", control_root=control_root)
    if not schema_validation.get("valid"):
        return schema_validation
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
        contract = completion_contract if isinstance(completion_contract, dict) else {}
        required_artifacts = contract.get("required_artifacts") if isinstance(contract.get("required_artifacts"), list) else []
        if required_artifacts and not payload.get("artifact_refs"):
            return validation_error(
                error_type="MissingArtifactRefs",
                path="$.artifact_refs",
                message="succeeded bridge result must include artifact_refs required by completion contract",
            )
    for index, report in enumerate(reports):
        validation = validate_teammate_report(report, path=f"$.reports[{index}]", strict=status != "failed", control_root=control_root)
        if not validation.get("valid"):
            return validation
    return validation_ok()


def validate_teammate_report(
    report: Any,
    *,
    path: str = "$",
    strict: bool = True,
    control_root: str | Path | None = None,
    required_sections: list[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(report, dict):
        return validation_error(error_type="InvalidTeammateReport", path=path, message="teammate report must be an object")
    if strict:
        schema_validation = validate_schema(report, "teammate_report.schema.json", control_root=control_root, root_path=path)
        if not schema_validation.get("valid"):
            return schema_validation
    if not report.get("summary"):
        return validation_error(error_type="MissingSummary", path=f"{path}.summary", message="teammate report requires summary")
    if strict:
        coverage = report.get("instruction_coverage")
        if not isinstance(coverage, dict):
            return validation_error(error_type="MissingInstructionCoverage", path=f"{path}.instruction_coverage", message="teammate report requires instruction_coverage")
        completed_claims = False
        for key, raw_disposition in coverage.items():
            disposition = _coverage_disposition(raw_disposition)
            if disposition not in COVERAGE_VALUES:
                return validation_error(
                    error_type="InvalidCoverageDisposition",
                    path=f"{path}.instruction_coverage.{_json_path_key(str(key))}",
                    message=f"invalid coverage disposition {disposition or raw_disposition}",
                )
            completed_claims = completed_claims or disposition == "completed"
        if not coverage:
            return validation_error(error_type="MissingInstructionCoverage", path=f"{path}.instruction_coverage", message="teammate report instruction_coverage cannot be empty")
        for section in required_sections or []:
            if section == "semantic_identity_resolution":
                resolution = report.get(section)
                if not isinstance(resolution, dict) or not resolution:
                    return validation_error(
                        error_type="MissingSemanticIdentityResolution",
                        path=f"{path}.semantic_identity_resolution",
                        message="teammate report requires semantic_identity_resolution from the report contract",
                    )
        completed_items = report.get("completed_items") if isinstance(report.get("completed_items"), list) else []
        completed_claims = completed_claims or bool(completed_items)
        evidence_refs = report.get("evidence_refs") if isinstance(report.get("evidence_refs"), list) else []
        evidence = report.get("evidence") if isinstance(report.get("evidence"), dict) else {}
        if completed_claims and not evidence_refs and not evidence:
            return validation_error(
                error_type="MissingRequiredEvidenceRef",
                path=f"{path}.evidence_refs",
                message="completed teammate report coverage requires evidence_refs or evidence",
            )
    return validation_ok()


def validate_completion_report(payload: Any, *, control_root: str | Path | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return validation_error(error_type="InvalidCompletionReport", path="$", message="completion report must be an object")
    schema_validation = validate_schema(payload, "completion_report.schema.json", control_root=control_root)
    if not schema_validation.get("valid"):
        return schema_validation
    checks = payload.get("completion_checks") if isinstance(payload.get("completion_checks"), dict) else {}
    if not checks:
        return validation_error(error_type="MissingCompletionChecks", path="$.completion_checks", message="completion report requires completion_checks")
    contract = payload.get("completion_contract") if isinstance(payload.get("completion_contract"), dict) else {}
    required_artifacts = contract.get("required_artifacts") if isinstance(contract.get("required_artifacts"), list) else []
    if checks.get("required_outputs_present") and not payload.get("reports"):
        return validation_error(error_type="MissingReportEvidence", path="$.reports", message="completion output claims reports exist but reports are missing")
    if required_artifacts and checks.get("required_artifacts_present") and not payload.get("artifact_refs"):
        return validation_error(error_type="MissingArtifactRefs", path="$.artifact_refs", message="completion output claims artifacts checked but artifact_refs are absent")
    for index, report in enumerate(payload.get("reports", []) if isinstance(payload.get("reports"), list) else []):
        validation = validate_teammate_report(report, path=f"$.reports[{index}]", strict=True, control_root=control_root)
        if not validation.get("valid"):
            return validation
    if checks.get("validation_passed") and not _has_completion_evidence(payload):
        return validation_error(
            error_type="MissingRequiredEvidenceRef",
            path="$.completion_evidence",
            message="completion report claims validation_passed but has no completion_evidence, report evidence_refs, or artifact_refs",
        )
    return validation_ok()


def validate_log_manifest(
    payload: Any,
    *,
    control_root: str | Path | None = None,
    formal_run: bool | None = None,
    required_fields: list[str] | None = None,
    formal_required_fields: list[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return validation_error(error_type="InvalidLogManifest", path="$", message="log manifest must be an object")
    schema_validation = validate_schema(payload, "log_manifest.schema.json", control_root=control_root)
    if not schema_validation.get("valid"):
        return schema_validation
    required = required_fields if isinstance(required_fields, list) and required_fields else _log_manifest_required_fields()
    for field in required:
        if field not in payload or _is_empty_value(payload.get(field)):
            return validation_error(error_type="MissingLogManifestField", path=f"$.{field}", message=f"log manifest missing {field}")
    environment_evidence = payload.get("environment_evidence")
    if not isinstance(environment_evidence, dict) or not environment_evidence:
        return validation_error(error_type="MissingLogManifestField", path="$.environment_evidence", message="log manifest requires non-empty environment_evidence")
    process_refs = payload.get("process_refs")
    if not isinstance(process_refs, list) or not process_refs:
        return validation_error(error_type="MissingLogManifestField", path="$.process_refs", message="log manifest requires non-empty process_refs")
    if formal_run is True or (formal_run is None and _looks_like_l4_formal_manifest(payload)):
        formal_fields = formal_required_fields if isinstance(formal_required_fields, list) else _formal_l4_manifest_required_fields()
        for field in formal_fields:
            if field not in payload or _is_empty_value(payload.get(field)):
                return validation_error(
                    error_type="MissingFormalRunEvidence",
                    path=f"$.{field}",
                    message=f"L4 execute formal log manifest missing {field}",
                )
    return validation_ok()


def load_schema(control_root: str | Path, schema_name: str) -> dict[str, Any]:
    root = Path(control_root).expanduser().resolve()
    return load_json_file(root / "schemas" / schema_name, default={}) or {}


def validate_schema(
    payload: Any,
    schema_name: str,
    *,
    control_root: str | Path | None = None,
    root_path: str = "$",
) -> dict[str, Any]:
    # Minimal local schema checker for runtime guardrails. It covers type,
    # required, enum, object properties, and array items only; it is not a
    # complete JSON Schema implementation.
    schema = _load_schema_default(control_root, schema_name)
    if not schema:
        return validation_ok()
    return _validate_schema_node(payload, schema, root_path)


def _load_schema_default(control_root: str | Path | None, schema_name: str) -> dict[str, Any]:
    if control_root is not None:
        return load_schema(control_root, schema_name)
    schema_root = Path(__file__).resolve().parents[1] / "schemas"
    return load_json_file(schema_root / schema_name, default={}) or {}


def _validate_schema_node(value: Any, schema: dict[str, Any], path: str) -> dict[str, Any]:
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for index, child_schema in enumerate(all_of):
            if isinstance(child_schema, dict):
                child = _validate_schema_node(value, child_schema, path)
                if not child.get("valid"):
                    return validation_error(
                        error_type="SchemaValidationFailed",
                        path=path,
                        message=f"{path} failed allOf[{index}]: {child.get('message')}",
                    )
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        if not any(isinstance(child_schema, dict) and _validate_schema_node(value, child_schema, path).get("valid") for child_schema in any_of):
            return validation_error(error_type="SchemaValidationFailed", path=path, message=f"{path} does not match any allowed schema")
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        matches = sum(1 for child_schema in one_of if isinstance(child_schema, dict) and _validate_schema_node(value, child_schema, path).get("valid"))
        if matches != 1:
            return validation_error(error_type="SchemaValidationFailed", path=path, message=f"{path} must match exactly one schema")
    expected_type = schema.get("type")
    if expected_type is not None and not _schema_type_matches(value, expected_type):
        return validation_error(
            error_type="SchemaValidationFailed",
            path=path,
            message=f"{path} expected type {expected_type}",
        )
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return validation_error(error_type="SchemaValidationFailed", path=path, message=f"{path} value is not in schema enum")
    if isinstance(value, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            return validation_error(error_type="SchemaValidationFailed", path=path, message=f"{path} is shorter than minLength")
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and len(value) > max_length:
            return validation_error(error_type="SchemaValidationFailed", path=path, message=f"{path} is longer than maxLength")
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                if re.search(pattern, value) is None:
                    return validation_error(error_type="SchemaValidationFailed", path=path, message=f"{path} does not match schema pattern")
            except re.error:
                return validation_error(error_type="SchemaValidationFailed", path=path, message=f"{path} has invalid schema pattern")
    if isinstance(value, dict):
        for key in schema.get("required", []) if isinstance(schema.get("required"), list) else []:
            if key not in value:
                return validation_error(error_type="SchemaValidationFailed", path=f"{path}.{key}", message=f"{path} missing required field {key}")
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for key, child_schema in properties.items():
            if key in value and isinstance(child_schema, dict):
                child = _validate_schema_node(value[key], child_schema, f"{path}.{key}")
                if not child.get("valid"):
                    return child
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            for key, item in value.items():
                if key in properties:
                    continue
                child = _validate_schema_node(item, additional, f"{path}.{_json_path_key(str(key))}")
                if not child.get("valid"):
                    return child
        elif additional is False:
            for key in value:
                if key not in properties:
                    return validation_error(
                        error_type="SchemaValidationFailed",
                        path=f"{path}.{_json_path_key(str(key))}",
                        message=f"{path} has unexpected field {key}",
                    )
    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            return validation_error(error_type="SchemaValidationFailed", path=path, message=f"{path} has fewer items than minItems")
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            return validation_error(error_type="SchemaValidationFailed", path=path, message=f"{path} has more items than maxItems")
        items_schema = schema.get("items") if isinstance(schema.get("items"), dict) else None
        if items_schema:
            for index, item in enumerate(value):
                child = _validate_schema_node(item, items_schema, f"{path}[{index}]")
                if not child.get("valid"):
                    return child
    return validation_ok()


def _schema_type_matches(value: Any, expected_type: Any) -> bool:
    if isinstance(expected_type, list):
        return any(_schema_type_matches(value, item) for item in expected_type)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return True


def _coverage_disposition(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("disposition", "status", "state"):
            if value.get(key):
                return str(value.get(key))
        return ""
    return str(value)


def _json_path_key(key: str) -> str:
    if key.replace("_", "").replace("-", "").isalnum():
        return key
    return json.dumps(key, ensure_ascii=False)


def _has_completion_evidence(payload: dict[str, Any]) -> bool:
    if payload.get("completion_evidence"):
        return True
    artifact_refs = payload.get("artifact_refs") if isinstance(payload.get("artifact_refs"), list) else []
    if artifact_refs:
        return True
    reports = payload.get("reports") if isinstance(payload.get("reports"), list) else []
    for report in reports:
        if not isinstance(report, dict):
            continue
        evidence_refs = report.get("evidence_refs") if isinstance(report.get("evidence_refs"), list) else []
        if evidence_refs or isinstance(report.get("evidence"), dict):
            return True
    return False


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


def _formal_l4_manifest_required_fields() -> list[str]:
    return [
        "batchbasis",
        "gpu_id_or_device_ids",
        "formal_memory_observed",
        "model_or_model_family",
        "dataset_name_split_source",
        "method_or_objective",
    ]


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _looks_like_l4_formal_manifest(payload: dict[str, Any]) -> bool:
    phase = str(payload.get("phase") or payload.get("target_phase") or "").lower()
    stage = " ".join(
        str(payload.get(key) or "").lower()
        for key in ("stage_name", "stage_kind", "run_kind", "execution_kind", "mode")
    )
    command = str(payload.get("command") or "").lower()
    explicit = payload.get("formal_run") is True or payload.get("is_formal") is True
    return bool(
        explicit
        or (phase == "l4_execute" and "formal" in stage)
        or ("l4_execute" in stage and "formal" in stage)
        or ("--formal" in command or " formal" in command)
    )
