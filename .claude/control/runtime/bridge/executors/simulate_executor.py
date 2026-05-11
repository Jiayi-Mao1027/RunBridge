from __future__ import annotations

from typing import Any

from claude_cli_executor import simulated_team_executor

from .base import BridgeExecutionRequest, BridgeEventSink


class SimulateBridgeExecutor:
    name = "simulate"

    def execute(
        self,
        request: BridgeExecutionRequest,
        *,
        event_sink: BridgeEventSink | None = None,
    ) -> dict[str, Any]:
        return simulated_team_executor(request.as_legacy_execution_input())

