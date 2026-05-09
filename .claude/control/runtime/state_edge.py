from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class StateEdge:
    """One native RunBridge state-graph edge."""

    edge_id: str
    source: str
    target: str
    event_kinds: tuple[str, ...] = ()
    phase_routes: tuple[tuple[str, str], ...] = ()
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StateEdge":
        edge_id = str(payload.get("id") or payload.get("edge_id") or "").strip()
        source = str(payload.get("from") or payload.get("source") or "").strip()
        target = str(payload.get("to") or payload.get("target") or "").strip()
        if not edge_id:
            edge_id = f"{source}->{target}"
        if not source or not target:
            raise ValueError(f"state graph edge {edge_id!r} requires source and target")
        routes = []
        for item in payload.get("phase_routes", []) if isinstance(payload.get("phase_routes"), list) else []:
            if isinstance(item, list) and len(item) == 2:
                routes.append((str(item[0]), str(item[1])))
            elif isinstance(item, dict):
                routes.append((str(item.get("from")), str(item.get("to"))))
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        return cls(
            edge_id=edge_id,
            source=source,
            target=target,
            event_kinds=tuple(str(item) for item in payload.get("event_kinds", []) if str(item)),
            phase_routes=tuple(routes),
            description=str(payload.get("description") or ""),
            metadata=dict(metadata),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.edge_id,
            "from": self.source,
            "to": self.target,
            "event_kinds": list(self.event_kinds),
            "phase_routes": [[source, target] for source, target in self.phase_routes],
            "description": self.description,
            "metadata": dict(self.metadata),
        }
