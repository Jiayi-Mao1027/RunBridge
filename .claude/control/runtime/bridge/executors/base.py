from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class BridgeEventSink(Protocol):
    def __call__(
        self,
        event_kind: str,
        *,
        payload: dict[str, Any] | None = None,
        agent_id: str | None = None,
        agent_type: str = "bridge-leader",
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        ...


@dataclass(frozen=True, slots=True)
class BridgeExecutionContext:
    run_id: str
    main_session_id: str
    sub_session_id: str
    bridge_window_id: str
    team_id: str
    task_id: str
    repo_key: str | None = None
    target_phase: str | None = None

    @classmethod
    def from_execution_input(cls, execution_input: dict[str, Any]) -> "BridgeExecutionContext":
        packet = execution_input.get("packet") if isinstance(execution_input.get("packet"), dict) else {}
        binding = packet.get("binding") if isinstance(packet.get("binding"), dict) else {}
        return cls(
            run_id=str(execution_input.get("run_id") or binding.get("run_id") or ""),
            main_session_id=str(execution_input.get("main_session_id") or binding.get("main_session_id") or ""),
            sub_session_id=str(execution_input.get("sub_session_id") or binding.get("sub_session_id") or ""),
            bridge_window_id=str(execution_input.get("bridge_window_id") or binding.get("bridge_window_id") or ""),
            team_id=str(execution_input.get("team_id") or ""),
            task_id=str(execution_input.get("task_id") or ""),
            repo_key=str(packet.get("repo_key") or binding.get("repo_key") or "") or None,
            target_phase=str(packet.get("target_phase") or "") or None,
        )

    def as_legacy_execution_input(self, packet: dict[str, Any]) -> dict[str, Any]:
        return {
            "packet": packet,
            "run_id": self.run_id,
            "main_session_id": self.main_session_id,
            "sub_session_id": self.sub_session_id,
            "bridge_window_id": self.bridge_window_id,
            "team_id": self.team_id,
            "task_id": self.task_id,
        }


@dataclass(frozen=True, slots=True)
class BridgeExecutionRequest:
    packet: dict[str, Any]
    context: BridgeExecutionContext

    @classmethod
    def from_execution_input(cls, execution_input: dict[str, Any]) -> "BridgeExecutionRequest":
        packet = execution_input.get("packet")
        if not isinstance(packet, dict):
            packet = {}
        return cls(packet=packet, context=BridgeExecutionContext.from_execution_input(execution_input))

    def as_legacy_execution_input(self) -> dict[str, Any]:
        return self.context.as_legacy_execution_input(self.packet)


class BridgeExecutor(Protocol):
    name: str

    def execute(
        self,
        request: BridgeExecutionRequest,
        *,
        event_sink: BridgeEventSink | None = None,
    ) -> dict[str, Any]:
        ...

