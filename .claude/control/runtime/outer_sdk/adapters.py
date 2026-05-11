from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


OuterLeaderEventSink = Callable[..., None]


class OuterLeaderAdapter(Protocol):
    name: str

    def handle_user_input(
        self,
        request: dict[str, Any],
        *,
        event_sink: OuterLeaderEventSink | None = None,
    ) -> dict[str, Any]:
        """Handle one user input inside the long-lived outer leader session."""
        ...


class UnavailableOuterLeaderAdapter:
    """Explicit migration placeholder for the real outer Claude SDK session."""

    name = "unavailable"

    def handle_user_input(
        self,
        request: dict[str, Any],
        *,
        event_sink: OuterLeaderEventSink | None = None,
    ) -> dict[str, Any]:
        return {
            "status": "blocked",
            "handled_by": self.name,
            "reports": [],
            "artifact_refs": [],
            "evidence": {
                "outer_sdk_migration_point": "outer_sdk.adapters.OuterLeaderAdapter",
                "run_id": request.get("run_id"),
                "repo_key": request.get("repo_key"),
            },
            "error_or_null": {
                "type": "OuterLeaderSdkNotConfigured",
                "message": "Outer leader SDK session is not configured. The host recorded the user input and runtime event, but did not run leader reasoning.",
            },
            "cleanup_required": False,
        }


def build_outer_leader_adapter(config: Any, *, mode: str | None = None) -> OuterLeaderAdapter:
    normalized = str(mode or "auto").strip().lower()
    if normalized in {"", "auto", "sdk", "claude-agent-sdk", "claude_agent_sdk"}:
        from .claude_agent_adapter import ClaudeAgentSdkOuterLeaderAdapter

        return ClaudeAgentSdkOuterLeaderAdapter(config)
    if normalized in {"unavailable", "disabled", "placeholder"}:
        return UnavailableOuterLeaderAdapter()
    raise ValueError(f"unknown outer leader adapter mode: {mode}")
