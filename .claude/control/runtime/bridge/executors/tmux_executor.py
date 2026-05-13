from __future__ import annotations

from pathlib import Path
import os
from typing import Any

from claude_cli_executor import claude_tmux_team_executor
from claude_cli_executor import should_use_tmux_bridge_executor

from .base import BridgeExecutionRequest, BridgeEventSink
from .cli_executor import CliBridgeExecutor


class TmuxBridgeExecutor:
    """Interactive Claude Code bridge executor.

    This is used for custom-provider Claude Code installations where the
    headless print-mode API path can fail while the real TTY entrypoint works.
    """

    name = "tmux"

    def execute(
        self,
        request: BridgeExecutionRequest,
        *,
        event_sink: BridgeEventSink | None = None,
    ) -> dict[str, Any]:
        return claude_tmux_team_executor(request.as_legacy_execution_input())


class AutoBridgeExecutor:
    """Select the safest bridge executor for the active runtime."""

    name = "auto"

    def execute(
        self,
        request: BridgeExecutionRequest,
        *,
        event_sink: BridgeEventSink | None = None,
    ) -> dict[str, Any]:
        project_root = Path(os.environ.get("BRIDGE_PROJECT_ROOT") or Path.cwd()).resolve()
        if should_use_tmux_bridge_executor(project_root):
            return TmuxBridgeExecutor().execute(request, event_sink=event_sink)
        return CliBridgeExecutor().execute(request, event_sink=event_sink)
