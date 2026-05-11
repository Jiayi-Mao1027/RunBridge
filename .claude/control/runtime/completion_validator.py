from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from artifact_refs import normalize_artifact_refs, validate_artifact_refs
from output_guardrails import validate_bridge_result, validate_teammate_report


PASS = "pass"
WARN = "warn"
FAIL = "fail"
BLOCK = "block"


def validate_bridge_completion(
    packet: dict[str, Any],
    execution: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
    control_root: str | Path | None = None,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    context = context or {}
    contract = packet.get("completion_contract") if isinstance(packet.get("completion_contract"), dict) else {}
    task_spec = packet.get("task_spec") if isinstance(packet.get("task_spec"), dict) else {}
    report_contract = packet.get("report_contract") if isinstance(packet.get("report_contract"), dict) else {}
    checks: list[dict[str, Any]] = []

    schema = validate_bridge_result(execution, control_root=control_root, completion_contract=contract)
    checks.append(_check("schema_validation", PASS if schema.get("valid") else BLOCK, schema.get("message") or "bridge result schema", evidence_ref=schema.get("path")))

    reports = execution.get("reports") if isinstance(execution.get("reports"), list) else []
    required_outputs = contract.get("required_outputs") if isinstance(contract.get("required_outputs"), list) else []
    if required_outputs and not reports:
        checks.append(_check("required_output", BLOCK, "report", message="required report output missing"))
    else:
        checks.append(_check("required_output", PASS, "report", evidence_ref=_report_evidence_ref(reports)))

    for index, report in enumerate(reports):
        validation = validate_teammate_report(report, path=f"$.reports[{index}]", strict=bool(execution.get("status") != "failed"), control_root=control_root)
        checks.append(_check("report_schema_validation", PASS if validation.get("valid") else FAIL, f"reports[{index}]", message=validation.get("message") or "", evidence_ref=validation.get("path")))

    artifact_validation = validate_artifact_refs(
        execution.get("artifact_refs"),
        required_artifacts=contract.get("required_artifacts"),
        context=context,
        base_dir=base_dir,
    )
    checks.extend(
        _check(
            f"artifact_{item.get('name')}",
            item.get("status", WARN),
            item.get("subject", "artifact"),
            message=item.get("message") or "",
            evidence_ref=item.get("evidence_ref"),
        )
        for item in artifact_validation.get("checks", [])
    )

    validation_requirements = contract.get("validation_requirements") if isinstance(contract.get("validation_requirements"), list) else []
    if execution.get("validation_passed") is False:
        checks.append(_check("contract_validation", BLOCK, "validation_passed", message="executor explicitly reported validation_passed=false"))
    for requirement in validation_requirements:
        checks.append(_validation_requirement_check(str(requirement), execution, artifact_validation))

    checks.extend(_coverage_checks(task_spec, reports))
    checks.extend(_report_contract_checks(report_contract, reports))
    checks.extend(_lifecycle_checks(packet, execution))
    checks.extend(_failure_disposition_checks(execution))

    blocking = [item for item in checks if item.get("status") in {FAIL, BLOCK}]
    final_disposition = _final_disposition(execution, blocking)
    missing_outputs = [] if reports else [str(item) for item in required_outputs]
    missing_artifacts = artifact_validation.get("missing_required_artifacts", [])
    failed_validations = [
        str(item.get("subject") or item.get("name"))
        for item in checks
        if item.get("name") in {"contract_validation", "manifest_validation"} and item.get("status") in {FAIL, BLOCK}
    ]
    return {
        "validated_by": "completion_validator.v1",
        "final_disposition": final_disposition,
        "required_outputs_present": not missing_outputs,
        "required_artifacts_present": not missing_artifacts,
        "validation_passed": not failed_validations and not any(item.get("status") == BLOCK for item in checks if str(item.get("name", "")).startswith("lifecycle")),
        "missing_outputs": missing_outputs,
        "missing_artifacts": missing_artifacts,
        "failed_validations": failed_validations,
        "checks": checks,
        "artifact_refs_normalized": artifact_validation.get("normalized_refs", []),
        "notes": [item.get("message") for item in checks if item.get("status") == WARN and item.get("message")],
    }


def completion_succeeded(validation: dict[str, Any]) -> bool:
    return validation.get("final_disposition") == "succeeded" and not any(
        item.get("status") in {FAIL, BLOCK} for item in validation.get("checks", []) if isinstance(item, dict)
    )


def _coverage_checks(task_spec: dict[str, Any], reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checklist = task_spec.get("instruction_coverage_checklist") if isinstance(task_spec.get("instruction_coverage_checklist"), list) else []
    if not checklist:
        return [_check("semantic_coverage", WARN, "instruction_coverage_checklist", message="packet has no instruction coverage checklist")]
    checks: list[dict[str, Any]] = []
    coverage = _merged_coverage(reports)
    accepted = {"completed", "deferred", "blocked", "escalated"}
    for item in checklist:
        key = str(item)
        disposition = _coverage_disposition(coverage.get(key))
        if not disposition:
            disposition = _fuzzy_coverage_disposition(key, coverage)
        if disposition in accepted:
            checks.append(_check("semantic_coverage", PASS, key, evidence_ref=f"coverage:{key}"))
        else:
            checks.append(_check("semantic_coverage", FAIL, key, message="coverage item missing or has invalid disposition"))
    return checks


def _report_contract_checks(report_contract: dict[str, Any], reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    required_sections = [str(item) for item in report_contract.get("required_sections", [])] if isinstance(report_contract.get("required_sections"), list) else []
    for section in required_sections:
        if section in {"summary", "instruction_coverage"}:
            if any(report.get(section) for report in reports if isinstance(report, dict)):
                checks.append(_check("report_contract", PASS, section))
            else:
                checks.append(_check("report_contract", FAIL, section, message="required report section missing"))
        elif section == "semantic_identity_resolution":
            if any(isinstance(report.get(section), dict) and report.get(section) for report in reports if isinstance(report, dict)):
                checks.append(_check("report_contract", PASS, section))
            else:
                checks.append(_check("report_contract", WARN, section, message="semantic identity section not present in reports"))
        else:
            if any(section in report for report in reports if isinstance(report, dict)):
                checks.append(_check("report_contract", PASS, section))
            else:
                checks.append(_check("report_contract", WARN, section, message="required section not explicitly present"))
    return checks


def _lifecycle_checks(packet: dict[str, Any], execution: dict[str, Any]) -> list[dict[str, Any]]:
    if str(packet.get("target_phase") or "") != "l4_execute":
        return []
    refs = execution.get("owned_process_refs") if isinstance(execution.get("owned_process_refs"), list) else []
    if execution.get("waiting") and not refs:
        return [_check("lifecycle_terminality", BLOCK, "team_idle", message="L4 execute cannot complete while executor reports waiting")]
    running = [ref for ref in refs if _process_ref_looks_running(ref)]
    if running:
        return [_check("lifecycle_terminality", BLOCK, "owned_process_refs", message="owned process refs are not terminal")]
    return [_check("lifecycle_terminality", PASS, "owned_process_refs")]


def _failure_disposition_checks(execution: dict[str, Any]) -> list[dict[str, Any]]:
    status = str(execution.get("status") or "")
    if status == "succeeded" and execution.get("error_or_null"):
        return [_check("failure_disposition", FAIL, "error_or_null", message="succeeded result carries error_or_null")]
    if status in {"partial", "partial_or_failed"}:
        return [_check("failure_disposition", WARN, status, message="partial result must not be reported as succeeded")]
    if status == "failed":
        return [_check("failure_disposition", FAIL, status, message="failed result cannot satisfy completion")]
    return [_check("failure_disposition", PASS, status or "unknown")]


def _validation_requirement_check(requirement: str, execution: dict[str, Any], artifact_validation: dict[str, Any]) -> dict[str, Any]:
    lowered = requirement.casefold()
    refs = artifact_validation.get("normalized_refs", []) if isinstance(artifact_validation.get("normalized_refs"), list) else []
    if "manifest" in lowered:
        if any("manifest" in json.dumps(ref, ensure_ascii=False, default=str).casefold() for ref in refs):
            if "field" in lowered:
                return _check("manifest_validation", PASS if _manifest_field_evidence_present(execution) else BLOCK, requirement, message="" if _manifest_field_evidence_present(execution) else "manifest required-field evidence missing")
            return _check("manifest_validation", PASS, requirement)
        return _check("manifest_validation", BLOCK, requirement, message="manifest artifact ref missing")
    if execution.get("validation_passed") is False:
        return _check("contract_validation", BLOCK, requirement, message="validation requirement failed")
    return _check("contract_validation", PASS, requirement)


def _manifest_field_evidence_present(execution: dict[str, Any]) -> bool:
    text = json.dumps({"reports": execution.get("reports"), "evidence": execution.get("evidence")}, ensure_ascii=False, default=str).casefold()
    required_markers = ["run_id", "bridge_window_id", "task_id", "command", "cwd", "batchbasis", "gpu", "memory", "model", "dataset", "method"]
    return all(marker in text for marker in required_markers)


def _merged_coverage(reports: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for report in reports:
        if not isinstance(report, dict):
            continue
        coverage = report.get("instruction_coverage")
        if isinstance(coverage, dict):
            merged.update(coverage)
    return merged


def _coverage_disposition(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("disposition", "status", "state"):
            if value.get(key):
                return str(value.get(key)).strip()
        return ""
    return str(value or "").strip()


def _fuzzy_coverage_disposition(item: str, coverage: dict[str, Any]) -> str:
    target = item.casefold()
    for key, value in coverage.items():
        key_text = str(key).casefold()
        if target and (target in key_text or key_text in target):
            return _coverage_disposition(value)
    return ""


def _process_ref_looks_running(ref: Any) -> bool:
    if not isinstance(ref, dict):
        return True
    status = str(ref.get("status") or ref.get("state") or ref.get("process_status") or "").strip().lower()
    if not status:
        return True
    return status not in {"completed", "complete", "succeeded", "success", "failed", "failure", "exited", "stopped", "dead", "terminated", "terminal"}


def _report_evidence_ref(reports: list[dict[str, Any]]) -> str | None:
    for report in reports:
        if not isinstance(report, dict):
            continue
        refs = report.get("evidence_refs")
        if isinstance(refs, list) and refs:
            return str(refs[0])
    return None


def _final_disposition(execution: dict[str, Any], blocking: list[dict[str, Any]]) -> str:
    status = str(execution.get("status") or "")
    if status == "failed":
        return "failed"
    if any(item.get("status") == BLOCK for item in blocking):
        return "blocked"
    if blocking:
        return "partial"
    if status in {"partial", "partial_or_failed"}:
        return "partial"
    return "succeeded"


def _check(name: str, status: str, subject: str, *, message: str = "", evidence_ref: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "subject": subject,
        "message": message,
        "evidence_ref": evidence_ref,
    }

