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


class RiskBasedTeamSelector:
    """Select the smallest policy-valid team for a packet risk profile."""

    def select(
        self,
        *,
        target_phase: str,
        task_spec: dict[str, Any],
        policy_teammates: list[dict[str, Any]],
    ) -> TeamPlanDecision:
        risks = self._risk_profile(target_phase=target_phase, task_spec=task_spec)
        selected = self._select(target_phase, policy_teammates, risks)
        reason = "policy_default"
        if len(selected) < len(policy_teammates):
            reason = "risk_reduced_team"
        return TeamPlanDecision(risk_profile=risks, selected_teammates=selected, reason=reason)

    def _risk_profile(self, *, target_phase: str, task_spec: dict[str, Any]) -> dict[str, bool]:
        text = " ".join(
            str(task_spec.get(key) or "")
            for key in ("task_subject", "task_description", "original_user_instruction", "task_kind")
        ).casefold()
        semantic = task_spec.get("semantic_resolution_contract") if isinstance(task_spec.get("semantic_resolution_contract"), dict) else {}
        return {
            "ambiguity": any(term in text for term in ("ambiguous", "clarify", "unclear", "strategy", "plan")),
            "write_risk": target_phase in {"l4_implement", "l3_bridge"} and any(term in text for term in ("delete", "move", "refactor", "rewrite", "migration")),
            "external_fact_dependency": any(term in text for term in ("latest", "paper", "docs", "research", "web", "benchmark")),
            "execution_required": target_phase == "l4_execute",
            "failure_recovery_required": target_phase == "l4_anomaly" or any(term in text for term in ("failed", "partial", "orphan", "recovery", "anomaly")),
            "semantic_identity_uncertain": bool(semantic.get("required_identity_fields")) and any(term in text for term in ("checkpoint", "dataset", "model", "metric", "prompt", "config")),
        }

    def _select(self, target_phase: str, teammates: list[dict[str, Any]], risks: dict[str, bool]) -> list[dict[str, Any]]:
        if target_phase == "l2_advisory" and not risks["ambiguity"] and not risks["external_fact_dependency"]:
            return _named(teammates, {"chiefmate-a"}) or deepcopy(teammates[:1])
        if target_phase == "l4_anomaly" and not risks["failure_recovery_required"]:
            return _named(teammates, {"anomaly-analyst-a"}) or deepcopy(teammates[:1])
        if target_phase == "l3_bridge" and not risks["write_risk"] and not risks["semantic_identity_uncertain"]:
            return _named(teammates, {"preflight-initial"}) or deepcopy(teammates[:1])
        return deepcopy(teammates)


def _named(teammates: list[dict[str, Any]], names: set[str]) -> list[dict[str, Any]]:
    return [deepcopy(item) for item in teammates if isinstance(item, dict) and str(item.get("teammate_name") or "") in names]

