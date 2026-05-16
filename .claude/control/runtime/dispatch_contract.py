from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any


DISPATCH_CONTRACT_SCHEMA_VERSION = "dispatch_contract.v1"
BASE_AGENT_INPUT_KEYS = ["description", "prompt", "subagent_type"]
AGENT_INPUT_KEYS = list(BASE_AGENT_INPUT_KEYS)
GENERIC_MODEL_ALIASES = {"haiku", "opus", "sonnet"}
AGENT_TOOL_MODEL_SCHEMA_CARRIER = "sonnet"
AGENT_WRAPPER_AUTO_KEYS = {"isolation", "run_in_background"}


def build_agent_dispatch(subagent_type: str, description: str, prompt: str) -> dict[str, Any]:
    """Build the Agent tool payload fields that are owned by the runtime."""
    name = str(subagent_type or "").strip()
    return {
        "tool_name": "Agent",
        "subagent_type": name,
        "description": str(description or "").strip(),
        "prompt": str(prompt or "").strip(),
        "allowed_input_keys": list(AGENT_INPUT_KEYS),
    }


def build_dispatch_contract(packet: dict[str, Any]) -> dict[str, Any]:
    """Build the system-owned teammate dispatch contract from a final BridgePacket."""
    binding = packet.get("binding") if isinstance(packet.get("binding"), dict) else {}
    team_spec = packet.get("team_spec") if isinstance(packet.get("team_spec"), dict) else {}
    task_spec = packet.get("task_spec") if isinstance(packet.get("task_spec"), dict) else {}
    mapping = packet.get("task_team_mapping") if isinstance(packet.get("task_team_mapping"), dict) else {}

    team_id = _first_text(binding.get("team_id_or_null"), team_spec.get("team_id_or_null"), mapping.get("team_id_or_null"))
    task_id = _first_text(binding.get("task_id_or_null"), task_spec.get("task_id_or_null"), mapping.get("task_id_or_null"))
    teammate_specs = team_spec.get("teammate_specs") if isinstance(team_spec.get("teammate_specs"), list) else []
    assignments = mapping.get("teammate_assignments") if isinstance(mapping.get("teammate_assignments"), list) else []
    assignments_by_name = {
        str(item.get("teammate_name") or item.get("teammate") or item.get("agent_type") or "").strip(): item
        for item in assignments
        if isinstance(item, dict) and str(item.get("teammate_name") or item.get("teammate") or item.get("agent_type") or "").strip()
    }

    teammates: dict[str, dict[str, Any]] = {}
    allowed_names: list[str] = []
    for teammate in teammate_specs:
        if not isinstance(teammate, dict):
            continue
        name = str(teammate.get("teammate_name") or teammate.get("agent_type") or "").strip()
        if not name:
            continue
        assignment = assignments_by_name.get(name, {})
        prompt = str(assignment.get("assignment") or assignment.get("prompt") or "").strip()
        role = str(teammate.get("role") or "bridge teammate").strip()
        description = str(
            (assignment.get("agent_dispatch") if isinstance(assignment.get("agent_dispatch"), dict) else {}).get("description")
            or f"{name}: {role}"
        ).strip()
        dispatch = build_agent_dispatch(name, description, prompt)
        allowed_names.append(name)
        teammates[name] = {
            "teammate_name": name,
            "role": role,
            "allowed_tools": [str(item) for item in teammate.get("allowed_tools", []) if str(item)],
            "responsibilities": [str(item) for item in teammate.get("responsibilities", []) if str(item)],
            "assignment": prompt,
            "expected_output": assignment.get("expected_output"),
            "model_binding": {
                "source": ".claude/agents/<subagent_type>.md frontmatter",
                "model": _agent_frontmatter_model(name),
                "agent_tool_model_field": "system_payload_must_be_absent",
                "tolerated_schema_carrier": AGENT_TOOL_MODEL_SCHEMA_CARRIER,
            },
            "agent_dispatch": dispatch,
        }

    return {
        "schema_version": DISPATCH_CONTRACT_SCHEMA_VERSION,
        "source": "BridgePacket",
        "binding": {
            "run_id": binding.get("run_id"),
            "main_session_id": binding.get("main_session_id"),
            "sub_session_id": binding.get("sub_session_id"),
            "bridge_window_id": binding.get("bridge_window_id"),
            "team_id": team_id,
            "task_id": task_id,
        },
        "team_name": team_spec.get("team_name"),
        "task_subject": task_spec.get("task_subject"),
        "allowed_agent_subagent_types": allowed_names,
        "agent_call_policy": {
            "model_field": "must_be_absent",
            "model_source": ".claude/agents/<subagent_type>.md frontmatter",
            "allowed_input_keys": list(AGENT_INPUT_KEYS),
            "wrapper_auto_keys_tolerated": sorted(AGENT_WRAPPER_AUTO_KEYS),
            "generic_model_aliases_forbidden": sorted(GENERIC_MODEL_ALIASES),
            "agent_tool_model_schema_carrier": AGENT_TOOL_MODEL_SCHEMA_CARRIER,
        },
        "teammates": teammates,
    }


def validate_dispatch_contract(packet: dict[str, Any], contract: Any) -> list[str]:
    if not isinstance(contract, dict):
        return ["dispatch_contract_missing"]
    expected = build_dispatch_contract(packet)
    reasons: list[str] = []
    if contract.get("schema_version") != DISPATCH_CONTRACT_SCHEMA_VERSION:
        reasons.append("dispatch_contract_schema_invalid")
    for key in ("binding", "allowed_agent_subagent_types", "agent_call_policy", "teammates"):
        if contract.get(key) != expected.get(key):
            reasons.append(f"dispatch_contract_{key}_mismatch")
    return reasons


def validate_agent_call_against_dispatch_contract(
    packet: dict[str, Any],
    tool_input: dict[str, Any],
    binding: dict[str, Any] | None = None,
) -> list[str]:
    reasons: list[str] = []
    if not isinstance(tool_input, dict):
        return ["agent_dispatch_input_invalid"]
    if not isinstance(packet, dict) or not packet:
        reasons.append("dispatch_contract_missing")
        return reasons
    contract = packet.get("dispatch_contract")
    if not isinstance(contract, dict):
        reasons.append("dispatch_contract_missing")
        return reasons
    reasons.extend(validate_dispatch_contract(packet, contract))
    if reasons:
        return reasons

    subagent_type = str(tool_input.get("subagent_type") or "").strip()
    teammates = contract.get("teammates") if isinstance(contract.get("teammates"), dict) else {}
    expected = teammates.get(subagent_type) if subagent_type else None
    if not isinstance(expected, dict):
        reasons.append("agent_dispatch_subagent_type_not_in_contract")
        return reasons

    expected_dispatch = expected.get("agent_dispatch") if isinstance(expected.get("agent_dispatch"), dict) else {}
    allowed_keys = set(str(item) for item in expected_dispatch.get("allowed_input_keys", AGENT_INPUT_KEYS) if str(item))
    actual_keys = set(str(key) for key in tool_input.keys())
    core_actual_keys = actual_keys - AGENT_WRAPPER_AUTO_KEYS
    tolerated_actual_keys = set(AGENT_WRAPPER_AUTO_KEYS)
    if _is_agent_tool_model_schema_carrier(tool_input.get("model")):
        tolerated_actual_keys.add("model")
    unknown_extra_keys = actual_keys - allowed_keys - tolerated_actual_keys
    missing_keys = allowed_keys - actual_keys
    if unknown_extra_keys or missing_keys:
        reasons.append("agent_dispatch_input_keys_mismatch")

    actual_model = tool_input.get("model")
    if "model" in actual_keys and not _is_agent_tool_model_schema_carrier(actual_model):
        if _generic_model_alias(actual_model):
            reasons.append("agent_dispatch_model_alias_forbidden")
        else:
            reasons.append("agent_dispatch_model_override_forbidden")

    for key in sorted(allowed_keys):
        if key == "prompt":
            if _has_text(expected_dispatch.get("prompt")) and not _has_text(tool_input.get("prompt")):
                reasons.append("agent_dispatch_prompt_empty")
            continue
        if tool_input.get(key) != expected_dispatch.get(key):
            reasons.append(f"agent_dispatch_{key}_mismatch")
    if binding:
        expected_binding = contract.get("binding") if isinstance(contract.get("binding"), dict) else {}
        for field in ("run_id", "main_session_id", "sub_session_id", "bridge_window_id", "team_id", "task_id"):
            actual = binding.get(field)
            expected_value = expected_binding.get(field)
            if _has_text(expected_value) and not _has_text(actual):
                reasons.append(f"agent_dispatch_binding_{field}_missing")
            elif _has_text(actual) and _has_text(expected_value) and str(actual) != str(expected_value):
                reasons.append(f"agent_dispatch_binding_{field}_mismatch")
    return reasons


def agent_dispatches(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    teammates = contract.get("teammates") if isinstance(contract.get("teammates"), dict) else {}
    return {
        name: deepcopy(item.get("agent_dispatch"))
        for name, item in teammates.items()
        if isinstance(item, dict) and isinstance(item.get("agent_dispatch"), dict)
    }


def _first_text(*values: Any) -> str | None:
    for value in values:
        if _has_text(value):
            return str(value).strip()
    return None


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _generic_model_alias(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    return normalized if normalized in GENERIC_MODEL_ALIASES else None


def _is_agent_tool_model_schema_carrier(value: Any) -> bool:
    return isinstance(value, str) and value.strip().casefold() == AGENT_TOOL_MODEL_SCHEMA_CARRIER


def _agent_frontmatter_model(name: str) -> str | None:
    frontmatter = _load_agent_frontmatter(name)
    model = str(frontmatter.get("model") or "").strip()
    if not model or _generic_model_alias(model):
        return None
    return model


def _load_agent_frontmatter(name: str) -> dict[str, str]:
    path = Path(__file__).resolve().parents[2] / "agents" / f"{name}.md"
    if not path.exists():
        return {}
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    frontmatter: dict[str, str] = {}
    for raw_line in lines[1:]:
        if raw_line.strip() == "---":
            break
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition(":")
        if sep:
            frontmatter[key.strip()] = value.strip().strip("'\"")
    return frontmatter
