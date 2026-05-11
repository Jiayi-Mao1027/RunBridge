from __future__ import annotations

from typing import Any

from claude_cli_executor import claude_cli_team_executor

from .base import BridgeExecutionRequest, BridgeEventSink


class CliBridgeExecutor:
    """Claude CLI bridge executor.

    This wraps the existing stream-json subprocess path. The old implementation
    remains the fallback/debug/canary route while SDK-in-SDK execution is built.
    """

    name = "cli"

    def execute(
        self,
        request: BridgeExecutionRequest,
        *,
        event_sink: BridgeEventSink | None = None,
    ) -> dict[str, Any]:
        return claude_cli_team_executor(request.as_legacy_execution_input())

