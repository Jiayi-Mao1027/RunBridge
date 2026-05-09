from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class StateNode:
    """One native RunBridge state-graph node."""

    node_id: str
    description: str = ""
    node_type: str = "runtime"
    terminal: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StateNode":
        node_id = str(payload.get("id") or payload.get("node_id") or "").strip()
        if not node_id:
            raise ValueError("state graph node requires id")
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        return cls(
            node_id=node_id,
            description=str(payload.get("description") or ""),
            node_type=str(payload.get("type") or payload.get("node_type") or "runtime"),
            terminal=bool(payload.get("terminal", False)),
            metadata=dict(metadata),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.node_id,
            "description": self.description,
            "type": self.node_type,
            "terminal": self.terminal,
            "metadata": dict(self.metadata),
        }
