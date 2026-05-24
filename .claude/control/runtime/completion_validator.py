from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from artifact_refs import artifact_ref_is_log_manifest, normalize_artifact_refs, validate_artifact_refs
from output_guardrails import validate_bridge_result, validate_log_manifest, validate_teammate_report


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

    required_report_sections = _required_report_sections(report_contract)
    for index, report in enumerate(reports):
        validation = validate_teammate_report(
            report,
            path=f"$.reports[{index}]",
            strict=bool(execution.get("status") != "failed"),
            control_root=control_root,
            required_sections=required_report_sections,
        )
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
        checks.append(_validation_requirement_check(str(requirement), execution, artifact_validation, base_dir=base_dir, contract=contract, control_root=control_root))

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
        if not disposition:
            disposition = _coverage_narrative_disposition(key, reports)
        if disposition in accepted:
            checks.append(_check("semantic_coverage", PASS, key, evidence_ref=f"coverage:{key}"))
        else:
            checks.append(_check("semantic_coverage", FAIL, key, message="coverage item missing or has invalid disposition"))
    return checks


def _report_contract_checks(report_contract: dict[str, Any], reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    required_sections = [str(item) for item in report_contract.get("required_sections", [])] if isinstance(report_contract.get("required_sections"), list) else []
    hard_required_sections = {
        str(item)
        for item in report_contract.get("hard_required_sections", [])
        if isinstance(report_contract.get("hard_required_sections"), list) and str(item)
    }
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
                checks.append(_check("report_contract", FAIL, section, message="semantic identity section not present in reports"))
        else:
            if any(section in report for report in reports if isinstance(report, dict)):
                checks.append(_check("report_contract", PASS, section))
            else:
                status = FAIL if section in hard_required_sections else WARN
                message = "hard-required report section missing" if status == FAIL else "required section not explicitly present"
                checks.append(_check("report_contract", status, section, message=message))
    return checks


def _required_report_sections(report_contract: dict[str, Any]) -> list[str]:
    if not isinstance(report_contract.get("required_sections"), list):
        return []
    return [str(item) for item in report_contract.get("required_sections", []) if str(item)]


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


def _validation_requirement_check(
    requirement: str,
    execution: dict[str, Any],
    artifact_validation: dict[str, Any],
    *,
    base_dir: str | Path | None = None,
    contract: dict[str, Any] | None = None,
    control_root: str | Path | None = None,
) -> dict[str, Any]:
    lowered = requirement.casefold()
    refs = artifact_validation.get("normalized_refs", []) if isinstance(artifact_validation.get("normalized_refs"), list) else []
    if _looks_like_execution_handoff_requirement(lowered):
        handoff_ok, missing = _execution_handoff_evidence_present(execution)
        message = "" if handoff_ok else f"handoff evidence missing: {', '.join(missing[:12])}"
        return _check(
            "manifest_validation",
            PASS if handoff_ok else BLOCK,
            requirement,
            message=message,
            evidence_ref="report:formal_handoff" if handoff_ok else None,
        )
    if "manifest" in lowered:
        manifest_refs = [ref for ref in refs if _ref_looks_like_log_manifest(ref)]
        if manifest_refs:
            required_fields = []
            if isinstance(contract, dict) and isinstance(contract.get("manifest_required_fields"), list):
                required_fields = [str(item) for item in contract["manifest_required_fields"] if str(item)]
            manifest_ok, missing = _manifest_field_evidence_present(
                execution,
                refs=manifest_refs,
                base_dir=base_dir,
                required_fields=required_fields,
                control_root=control_root or Path(__file__).resolve().parents[1],
                formal_run=True,
            )
            message = "" if manifest_ok else "readable log manifest JSON with required fields missing"
            if missing:
                message = f"{message}: {', '.join(missing[:12])}"
            return _check("manifest_validation", PASS if manifest_ok else BLOCK, requirement, message=message)
        return _check("manifest_validation", BLOCK, requirement, message="manifest artifact ref missing")
    if execution.get("validation_passed") is False:
        return _check("contract_validation", BLOCK, requirement, message="validation requirement failed")
    return _check("contract_validation", PASS, requirement)


def _looks_like_execution_handoff_requirement(lowered_requirement: str) -> bool:
    return (
        "execution entrypoint" in lowered_requirement
        and "non-dry-run command" in lowered_requirement
        and ("data/input manifest" in lowered_requirement or "data/input manifests" in lowered_requirement)
    )


def _execution_handoff_evidence_present(execution: dict[str, Any]) -> tuple[bool, list[str]]:
    reports = execution.get("reports") if isinstance(execution.get("reports"), list) else []
    best_missing: list[str] | None = None
    for report in reports:
        if not isinstance(report, dict):
            continue
        handoff = report.get("formal_handoff") if isinstance(report.get("formal_handoff"), dict) else {}
        readiness = report.get("execution_readiness") if isinstance(report.get("execution_readiness"), dict) else {}
        if not handoff and not readiness:
            continue
        missing = _missing_execution_handoff_fields(handoff, readiness)
        if not missing:
            return True, []
        if best_missing is None or len(missing) < len(best_missing):
            best_missing = missing
    return False, best_missing or ["formal_handoff", "execution_readiness"]


def _missing_execution_handoff_fields(handoff: dict[str, Any], readiness: dict[str, Any]) -> list[str]:
    blocker = _first_handoff_text(handoff, readiness, "blocker_reason", "approved_blocker_reason", "blocked_reason")
    if blocker:
        return []

    commands = _handoff_text_values(
        handoff,
        "commands",
        "command",
        "formal_command",
        "execute_command",
        "command_sequence",
        "formal_non_dry_run_commands",
        "non_dry_run_commands",
        "formal_train_command",
        "formal_training_command",
    )
    commands.extend(_handoff_text_values(readiness, "commands", "command"))
    command_text = " ".join(commands)
    combined = " ".join(
        part
        for part in (
            _jsonish_text(handoff),
            _jsonish_text(readiness),
            command_text,
        )
        if part
    ).casefold()

    missing: list[str] = []
    if not commands:
        missing.append("formal_command")
    if not _first_handoff_text(handoff, readiness, "cwd", "workdir", "working_directory"):
        missing.append("cwd")
    if "config" not in combined and "configs/" not in combined:
        missing.append("configs")
    if not any(marker in combined for marker in ("input_manifest", "input-manifest", "data_input", "data/input", "split_manifest", "manifest.jsonl")):
        missing.append("data_input_manifest")
    if not any(marker in combined for marker in ("expected_output", "expected_outputs", "expected_artifact", "expected_artifacts", "outputs/")):
        missing.append("expected_outputs")
    if any(_command_claims_dry_run(command) for command in commands):
        missing.append("non_dry_run_command")
    return missing


def _handoff_text_values(source: dict[str, Any], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    values.append(item.strip())
    return values


def _first_handoff_text(source_a: dict[str, Any], source_b: dict[str, Any], *keys: str) -> str:
    sources = (source_a, source_b)
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _jsonish_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value or "")


def _command_claims_dry_run(command: str) -> bool:
    text = str(command or "").casefold()
    if "dry_run_only=true" in text or "dry-run-only=true" in text:
        return True
    if "--dry-run=false" in text or "--dry_run=false" in text or "--dry-run 0" in text or "--dry_run 0" in text:
        return False
    return "--dry-run" in text or "--dry_run" in text


def _manifest_field_evidence_present(
    execution: dict[str, Any],
    *,
    refs: list[dict[str, Any]] | None = None,
    base_dir: str | Path | None = None,
    required_fields: list[str] | None = None,
    control_root: str | Path | None = None,
    formal_run: bool | None = None,
) -> tuple[bool, list[str]]:
    ref_missing = _manifest_missing_fields_from_refs(
        refs or [],
        base_dir=base_dir,
        required_fields=required_fields or [],
        control_root=control_root,
        formal_run=formal_run,
    )
    if ref_missing is not None:
        return (not ref_missing, ref_missing)
    return False, ["readable_log_manifest_json"]


def _manifest_missing_fields_from_refs(
    refs: list[dict[str, Any]],
    *,
    base_dir: str | Path | None,
    required_fields: list[str],
    control_root: str | Path | None = None,
    formal_run: bool | None = None,
) -> list[str] | None:
    manifests: list[dict[str, Any]] = []
    invalid: list[str] = []
    for ref in refs:
        if not _ref_looks_like_log_manifest(ref):
            continue
        payload = _load_manifest_ref_payload(ref, base_dir=base_dir)
        if isinstance(payload, dict):
            validation = validate_log_manifest(
                payload,
                control_root=control_root,
                formal_run=formal_run,
                required_fields=required_fields,
                formal_required_fields=[],
            )
            if not validation.get("valid"):
                invalid.append(str(validation.get("path") or validation.get("message") or "invalid_log_manifest"))
                continue
            manifests.append(payload)
        else:
            invalid.append(str(ref.get("path") or ref.get("id") or "unreadable_log_manifest"))
    if not manifests:
        return invalid or None
    fields = required_fields or ["run_id", "bridge_window_id", "task_id", "command", "cwd", "terminal_status"]
    missing: list[str] = []
    for field in fields:
        if not any(not _missing_required_manifest_field(manifest, field) for manifest in manifests):
            missing.append(field)
    return missing


def _missing_required_manifest_field(manifest: dict[str, Any], field: str) -> bool:
    if field not in manifest:
        return not _static_only_manifest_allows_empty(manifest, field)
    return _empty_manifest_field(manifest, field)


def _ref_looks_like_log_manifest(ref: dict[str, Any]) -> bool:
    return artifact_ref_is_log_manifest(ref)


def _load_manifest_ref_payload(ref: dict[str, Any], *, base_dir: str | Path | None) -> dict[str, Any] | None:
    path_text = ref.get("path")
    if not path_text:
        return None
    path = Path(str(path_text)).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = Path(base_dir).expanduser().resolve() / path
    try:
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _empty_manifest_field(manifest: dict[str, Any], field: str) -> bool:
    value = manifest.get(field)
    if _static_only_manifest_allows_empty(manifest, field):
        return False
    return _empty_manifest_value(value)


def _empty_manifest_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


def _static_only_manifest_allows_empty(manifest: dict[str, Any], field: str) -> bool:
    if field == "failure_reason" and _manifest_terminal_status_succeeded(manifest):
        return True
    if field not in {"output_checkpoint_log_paths", "process_refs", "conda_env_evidence"}:
        return False
    text = " ".join(
        str(manifest.get(key) or "")
        for key in (
            "expected_outputs_or_checkpoints",
            "terminal_status",
            "batchbasis",
            "stage_name",
            "stage_kind",
            "run_kind",
            "execution_kind",
            "mode",
            "command",
        )
    ).casefold()
    static_only = (
        "static" in text
        or "no model" in text
        or "no checkpoint" in text
        or "no checkpoints" in text
        or "did not run" in text
        or "not_applicable" in text
        or "not applicable" in text
    )
    if not static_only:
        return False
    value = manifest.get(field)
    if field == "output_checkpoint_log_paths":
        return isinstance(value, list) and not value
    if field == "process_refs":
        return isinstance(value, list) and not value and bool(manifest.get("log_files"))
    if field == "conda_env_evidence":
        return _empty_manifest_value(value)
    return False


def _manifest_terminal_status_succeeded(manifest: dict[str, Any]) -> bool:
    status = str(manifest.get("terminal_status") or "").strip().casefold()
    if not status:
        return False
    failed_markers = ("fail", "error", "block", "reject", "timeout", "oom")
    if any(marker in status for marker in failed_markers):
        return False
    return any(marker in status for marker in ("pass", "success", "succeed", "complete", "completed", "done"))


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
    target = _coverage_match_key(item)
    target_tokens = _coverage_match_tokens(target)
    for key, value in coverage.items():
        key_text = _coverage_match_key(key)
        if target and (target in key_text or key_text in target):
            return _coverage_disposition(value)
        if _coverage_tokens_match(target_tokens, _coverage_match_tokens(key_text)):
            return _coverage_disposition(value)
    compound_disposition = _compound_negative_coverage_disposition(target, coverage)
    if compound_disposition:
        return compound_disposition
    return ""


def _coverage_narrative_disposition(item: str, reports: list[dict[str, Any]]) -> str:
    target_tokens = _coverage_match_tokens(_coverage_match_key(item))
    if len(target_tokens) < 5:
        return ""
    narrative_tokens = _coverage_match_tokens(_report_coverage_narrative(reports))
    if not narrative_tokens:
        return ""
    shared = target_tokens & narrative_tokens
    ratio = len(shared) / len(target_tokens)
    if len(shared) >= max(4, int(0.55 * len(target_tokens))):
        return "completed"
    if len(shared) >= 8 and ratio >= 0.45:
        return "completed"
    return ""


def _report_coverage_narrative(reports: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for report in reports:
        if not isinstance(report, dict):
            continue
        for key in ("summary", "next_action_recommendation", "classification"):
            value = report.get(key)
            if value:
                parts.append(str(value))
        for key in ("evidence", "current_user_intent_context"):
            value = report.get(key)
            if isinstance(value, dict):
                for nested in value.values():
                    if isinstance(nested, (str, int, float, bool)):
                        parts.append(str(nested))
    return " ".join(parts)


def _coverage_match_key(value: Any) -> str:
    text = " ".join(str(value or "").split()).casefold()
    text = re.sub(r"\s+([,.;:!?/)\]\}])", r"\1", text)
    text = re.sub(r"([(/])\s+", r"\1", text)
    return text.strip()


def _coverage_match_tokens(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", text.casefold().replace("_", " "))
    stopwords = {"a", "an", "the", "and", "or", "is", "are", "to", "with", "this", "that", "in", "has", "have", "only", "but"}
    aliases = {
        "implementation": "implement",
        "implemented": "implement",
        "implementing": "implement",
        "execution": "execute",
        "executed": "execute",
        "executing": "execute",
        "performed": "perform",
        "performing": "perform",
        "proceeding": "proceed",
        "proceeds": "proceed",
        "requires": "require",
        "required": "require",
        "artifacts": "artifact",
        "outputs": "output",
    }
    normalized: set[str] = set()
    for token in tokens:
        if token in stopwords:
            continue
        token = aliases.get(token, token)
        if len(token) > 4 and token.endswith("s"):
            token = token[:-1]
        normalized.add(token)
    return normalized


def _coverage_tokens_match(target: set[str], candidate: set[str]) -> bool:
    if len(target) < 4 or len(candidate) < 4:
        return False
    shared = target & candidate
    if len(shared) < max(3, int(0.6 * min(len(target), len(candidate)))):
        return False
    score = (2 * len(shared)) / (len(target) + len(candidate))
    return score >= 0.72


def _compound_negative_coverage_disposition(target: str, coverage: dict[str, Any]) -> str:
    clauses = _negative_coverage_clauses(target)
    if len(clauses) < 2:
        return ""

    accepted_dispositions = {"completed", "deferred", "blocked", "escalated"}
    accepted_entries: list[tuple[set[str], str]] = []
    for key, value in coverage.items():
        disposition = _coverage_disposition(value)
        if disposition not in accepted_dispositions:
            continue
        tokens = _coverage_match_tokens(_coverage_match_key(key))
        if len(tokens) >= 3:
            accepted_entries.append((tokens, disposition))
    if not accepted_entries:
        return ""

    dispositions: list[str] = []
    for clause in clauses:
        clause_tokens = _coverage_match_tokens(clause)
        if len(clause_tokens) < 3:
            continue
        matched = ""
        for entry_tokens, disposition in accepted_entries:
            if _coverage_clause_tokens_match(clause_tokens, entry_tokens):
                matched = disposition
                break
        if not matched:
            return ""
        dispositions.append(matched)

    if not dispositions:
        return ""
    if all(disposition == "completed" for disposition in dispositions):
        return "completed"
    if any(disposition == "blocked" for disposition in dispositions):
        return "blocked"
    if any(disposition == "escalated" for disposition in dispositions):
        return "escalated"
    return "deferred"


def _negative_coverage_clauses(target: str) -> list[str]:
    normalized = _coverage_match_key(target).replace("don't", "do not").strip(" ,.;:")
    if normalized.count("do not") >= 2:
        return [
            clause.strip(" ,;")
            for clause in re.split(r"(?:[,;]\s*|\s+and\s+)(?=do not\b)", normalized)
            if clause.strip(" ,;")
        ]

    match = re.search(r"\bdo not\s+(.+?)\s+or\s+([a-z0-9_]+)(?:\s+(.+))?$", normalized)
    if not match:
        return []
    left = match.group(1).strip(" ,;")
    right = match.group(2).strip(" ,;")
    shared_tail = (match.group(3) or "").strip(" ,;")
    left_tokens = _coverage_match_tokens(left)
    if not (1 <= len(left_tokens) <= 3):
        return []
    left_clause = " ".join(part for part in ("do not", left, shared_tail) if part).strip()
    right_clause = " ".join(part for part in ("do not", right, shared_tail) if part).strip()
    return [left_clause, right_clause]


def _coverage_clause_tokens_match(target: set[str], candidate: set[str]) -> bool:
    shared = target & candidate
    if len(shared) < max(3, int(0.55 * min(len(target), len(candidate)))):
        return False
    return len(shared) / len(target) >= 0.55


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

