from __future__ import annotations

from typing import Any

from .base import BridgeExecutionRequest, BridgeEventSink


class SdkBridgeExecutor:
    """SDK-in-SDK migration point.

    This is intentionally a skeleton. Selecting BRIDGE_EXECUTOR=sdk reports a
    clear unsupported executor result instead of pretending to run an inner SDK
    session. The interface is now stable enough for a real implementation to
    emit normalized inner-SDK stream events without touching bridge_leader again.
    """

    name = "sdk"

    def execute(
        self,
        request: BridgeExecutionRequest,
        *,
        event_sink: BridgeEventSink | None = None,
    ) -> dict[str, Any]:
        return {
            "status": "failed",
            "reports": [],
            "artifact_refs": [],
            "evidence": {
                "executor": self.name,
                "sdk_migration_point": "bridge.executors.sdk_executor.SdkBridgeExecutor",
                "bridge_window_id": request.context.bridge_window_id,
                "task_id": request.context.task_id,
            },
            "error_or_null": {
                "type": "SdkExecutorNotImplemented",
                "message": "Inner bridge SDK session executor is a migration skeleton; use BRIDGE_EXECUTOR=cli or simulate.",
            },
            "cleanup_required": False,
        }
