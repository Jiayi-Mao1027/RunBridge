from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


RISK_KEYS = {
    "ambiguity",
    "write_risk",
    "external_fact_dependency",
    "execution_required",
    "failure_recovery_required",
    "semantic_identity_uncertain",
}


@dataclass(frozen=True, slots=True)
class TeamPlanDecision:
    risk_profile: dict[str, bool]
    selected_teammates: list[dict[str, Any]]
    reason: str
    selector: str = "risk_based_team_selector.v1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "reason": self.reason,
            "risk_profile": dict(self.risk_profile),
            "selected_teammate_names": [
                str(item.get("teammate_name") or "")
                for item in self.selected_teammates
                if isinstance(item, dict) and item.get("teammate_name")
            ],
        }


class RiskBasedTeamSelector:
    """Select the smallest policy-valid team for a packet risk profile."""

    def select(
        self,
        *,
        target_phase: str,
        task_spec: dict[str, Any],
        policy_teammates: list[dict[str, Any]],
        config: dict[str, Any] | None = None,
    ) -> TeamPlanDecision:
        config = config if isinstance(config, dict) else {}
        risks = self._risk_profile(target_phase=target_phase, task_spec=task_spec, config=config)
        if config.get("enabled") is False:
            return TeamPlanDecision(risk_profile=risks, selected_teammates=deepcopy(policy_teammates), reason="planner_disabled")
        text = _task_text(task_spec)
        if _matches_any(text, _configured_terms(config, "force_full_team_markers", DEFAULT_FORCE_FULL_MARKERS)):
            return TeamPlanDecision(risk_profile=risks, selected_teammates=deepcopy(policy_teammates), reason="force_full_team_marker")
        selected = self._select(target_phase, policy_teammates, risks, config)
        reason = "policy_full_team"
        if len(selected) < len(policy_teammates):
            reason = "risk_reduced_team"
        return TeamPlanDecision(risk_profile=risks, selected_teammates=selected, reason=reason)

    def _risk_profile(self, *, target_phase: str, task_spec: dict[str, Any], config: dict[str, Any]) -> dict[str, bool]:
        text = _task_text(task_spec)
        semantic = task_spec.get("semantic_resolution_contract") if isinstance(task_spec.get("semantic_resolution_contract"), dict) else {}
        terms = config.get("risk_terms") if isinstance(config.get("risk_terms"), dict) else {}
        return {
            "ambiguity": _matches_any(text, _terms(terms, "ambiguity", ("ambiguous", "clarify", "unclear", "strategy", "plan"))),
            "write_risk": target_phase in {"l4_implement", "l3_bridge"} and _matches_any(
                text,
                _terms(terms, "write_risk", ("delete", "move", "refactor", "rewrite", "migration", "update", "edit", "write", "claude.md", "readme")),
            ),
            "external_fact_dependency": _matches_any(text, _terms(terms, "external_fact_dependency", ("latest", "paper", "research", "web", "benchmark"))),
            "execution_required": target_phase == "l4_execute",
            "failure_recovery_required": target_phase == "l4_anomaly" or _matches_any(text, _terms(terms, "failure_recovery_required", ("failed", "partial", "orphan", "recovery", "anomaly"))),
            "semantic_identity_uncertain": bool(semantic.get("required_identity_fields")) and _matches_any(text, _terms(terms, "semantic_identity_uncertain", ("checkpoint", "dataset", "model", "metric", "prompt", "config"))),
        }

    def _select(self, target_phase: str, teammates: list[dict[str, Any]], risks: dict[str, bool], config: dict[str, Any]) -> list[dict[str, Any]]:
        configured = _configured_phase_rule(config, target_phase)
        if configured:
            full_risks = [str(item) for item in configured.get("full_team_risks", []) if str(item)]
            reduced_names = {str(item) for item in configured.get("reduced_teammates", []) if str(item)}
            if reduced_names and not any(risks.get(risk) for risk in full_risks):
                return _named(teammates, reduced_names) or deepcopy(teammates[:1])
        if target_phase == "l2_advisory" and not risks["ambiguity"] and not risks["external_fact_dependency"]:
            return _named(teammates, {"chiefmate-a"}) or deepcopy(teammates[:1])
        if target_phase == "l4_anomaly" and not risks["failure_recovery_required"]:
            return _named(teammates, {"anomaly-analyst-a"}) or deepcopy(teammates[:1])
        if target_phase == "l3_bridge" and not risks["write_risk"] and not risks["semantic_identity_uncertain"]:
            return _named(teammates, {"preflight-initial"}) or deepcopy(teammates[:1])
        return deepcopy(teammates)


DEFAULT_FORCE_FULL_MARKERS = ("three-seat", "full team", "chiefmate", "multi-view", "high risk")


def _named(teammates: list[dict[str, Any]], names: set[str]) -> list[dict[str, Any]]:
    return [deepcopy(item) for item in teammates if isinstance(item, dict) and str(item.get("teammate_name") or "") in names]


def _task_text(task_spec: dict[str, Any]) -> str:
    chunks = [
        str(task_spec.get(key) or "")
        for key in ("task_subject", "task_description", "original_user_instruction", "task_kind")
    ]
    preserved = task_spec.get("preserved_task_context")
    if isinstance(preserved, dict):
        chunks.extend(str(value) for value in preserved.values())
    return " ".join(chunks).casefold()


def _terms(terms: dict[str, Any], key: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    configured = terms.get(key)
    if isinstance(configured, list) and configured:
        return tuple(str(item).casefold() for item in configured if str(item))
    return tuple(item.casefold() for item in defaults)


def _configured_terms(config: dict[str, Any], key: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    configured = config.get(key)
    if isinstance(configured, list) and configured:
        return tuple(str(item).casefold() for item in configured if str(item))
    return tuple(item.casefold() for item in defaults)


def _matches_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term and term in text for term in terms)


def _configured_phase_rule(config: dict[str, Any], target_phase: str) -> dict[str, Any]:
    rules = config.get("phase_rules") if isinstance(config.get("phase_rules"), dict) else {}
    rule = rules.get(target_phase)
    return rule if isinstance(rule, dict) else {}
