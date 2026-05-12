from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import shutil
from typing import Any, Protocol
from urllib.parse import urlsplit


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
    if normalized in {"", "auto"}:
        if _should_use_tmux_repl_for_auto(config):
            from .tmux_repl_adapter import TmuxReplOuterLeaderAdapter

            return TmuxReplOuterLeaderAdapter(config)
        from .claude_agent_adapter import ClaudeAgentSdkOuterLeaderAdapter

        return ClaudeAgentSdkOuterLeaderAdapter(config)
    if normalized in {"sdk", "claude-agent-sdk", "claude_agent_sdk"}:
        from .claude_agent_adapter import ClaudeAgentSdkOuterLeaderAdapter

        return ClaudeAgentSdkOuterLeaderAdapter(config)
    if normalized in {"tmux", "tmux-repl", "repl", "tty", "interactive"}:
        from .tmux_repl_adapter import TmuxReplOuterLeaderAdapter

        return TmuxReplOuterLeaderAdapter(config)
    if normalized in {"unavailable", "disabled", "placeholder"}:
        return UnavailableOuterLeaderAdapter()
    raise ValueError(f"unknown outer leader adapter mode: {mode}")


def _should_use_tmux_repl_for_auto(config: Any) -> bool:
    override = os.environ.get("BRIDGE_OUTER_LEADER_AUTO_TMUX") or os.environ.get("OUTER_LEADER_AUTO_TMUX")
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes", "on"}
    if os.name == "nt" or not shutil.which("tmux"):
        return False
    base_url = _settings_base_url(config)
    if not base_url:
        return False
    host = (urlsplit(base_url).hostname or "").lower()
    return bool(host and host not in {"api.anthropic.com", "claude.ai", "console.anthropic.com"})


def _settings_base_url(config: Any) -> str:
    control_root = Path(getattr(config, "control_root", "") or "").expanduser()
    repo_root = getattr(config, "repo_root", None)
    candidates: list[Path] = []
    if repo_root:
        candidates.append(Path(repo_root).expanduser().resolve().parent / ".claude" / "settings.json")
    if control_root:
        candidates.append(control_root.expanduser().resolve().parent / "settings.json")
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        env = payload.get("env") if isinstance(payload, dict) else None
        if isinstance(env, dict) and env.get("ANTHROPIC_BASE_URL"):
            return str(env["ANTHROPIC_BASE_URL"])
    return ""
