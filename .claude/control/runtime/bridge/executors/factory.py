from __future__ import annotations

import os

from .base import BridgeExecutor
from .cli_executor import CliBridgeExecutor
from .sdk_executor import SdkBridgeExecutor
from .simulate_executor import SimulateBridgeExecutor
from .tmux_executor import AutoBridgeExecutor, TmuxBridgeExecutor


def bridge_executor_from_env() -> BridgeExecutor:
    mode = os.environ.get("BRIDGE_EXECUTOR", "auto").strip().lower()
    if mode in {"", "auto"}:
        return AutoBridgeExecutor()
    if mode in {"simulate", "simulated", "smoke"}:
        return SimulateBridgeExecutor()
    if mode in {"sdk", "inner-sdk", "sdk-in-sdk"}:
        return SdkBridgeExecutor()
    if mode in {"tmux", "tty", "interactive", "claude-tmux"}:
        return TmuxBridgeExecutor()
    if mode in {"cli", "claude-cli", "claude_cli", "fallback", "debug", "canary"}:
        return CliBridgeExecutor()
    return CliBridgeExecutor()
