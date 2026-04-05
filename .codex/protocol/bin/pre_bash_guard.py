#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

RUNTIME_STATE = Path.home() / '.codex' / 'runtime_state'
OWNED_PATH = RUNTIME_STATE / 'process_guard' / 'owned.json'
GPU_PROBED_FLAG = RUNTIME_STATE / 'gpu_probed'

KILL_RE = re.compile(r'\b(kill|pkill|killall|skill)\b', re.IGNORECASE)
KILL_PID_RE = re.compile(r'\bkill\s+(?:-\d+\s+)?(\d+)', re.IGNORECASE)

DESTRUCTIVE_RE = re.compile(
    r'\brm\s+.*-[^\s]*r[^\s]*f|'
    r'\brm\s+-rf\b|'
    r'\bmkfs\b|'
    r'\bdd\s+.*of=/',
    re.IGNORECASE,
)

CRITICAL_PATHS = (
    'specs/', 'ops/', 'AGENTS.md', 'CLAUDE.md',
    '.codex/', '.claude/', '.agents/',
    '/etc/', '/usr/', '/home/',
)

GPU_LAUNCH_RE = re.compile(
    r'\btorchrun\b|'
    r'\bpython[^\n]*train\b|'
    r'\baccelerate\s+launch\b|'
    r'\bdeepspeed\b|'
    r'\bnvidia-smi\b|'
    r'\bCUDA_VISIBLE_DEVICES\b',
    re.IGNORECASE,
)


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def owned_pids() -> set[int]:
    payload = read_json(OWNED_PATH, {'items': []})
    return {
        int(item['pid'])
        for item in payload.get('items', [])
        if str(item.get('pid', '')).isdigit()
    }


def deny(reason: str) -> int:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))
    return 0


def warn(message: str) -> int:
    print(json.dumps({"systemMessage": message}, ensure_ascii=False))
    return 0


def check_kill_safety(command: str) -> str | None:
    if not KILL_RE.search(command):
        return None

    pids_in_cmd = {int(m) for m in KILL_PID_RE.findall(command)}
    if not pids_in_cmd:
        return None

    owned = owned_pids()
    foreign = pids_in_cmd - owned
    if foreign:
        return (
            f'Blocked: kill targets foreign PIDs {sorted(foreign)}. '
            f'Only owned PIDs may be killed. '
            f'Currently owned: {sorted(owned) if owned else "none"}.'
        )
    return None


def check_destructive(command: str) -> str | None:
    if not DESTRUCTIVE_RE.search(command):
        return None
    for crit in CRITICAL_PATHS:
        if crit in command:
            return (
                f'Blocked: destructive operation targets critical path "{crit}". '
                f'This requires explicit Layer-1 authorization.'
            )
    return None


def check_gpu_probe(command: str) -> str | None:
    if not GPU_LAUNCH_RE.search(command):
        return None
    if command.strip().startswith('nvidia-smi'):
        return None
    if not GPU_PROBED_FLAG.exists():
        return (
            'GPU launch detected but no GPU probe has been recorded in this session. '
            'Run gpu_probe.py or nvidia-smi first.'
        )
    return None


def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        hook_input = {}

    tool_input = hook_input.get('tool_input', {})
    if isinstance(tool_input, dict):
        command = tool_input.get('command', '') or ''
    else:
        command = str(tool_input or '')

    if not command:
        return 0

    reason = check_kill_safety(command)
    if reason:
        return deny(reason)

    reason = check_destructive(command)
    if reason:
        return deny(reason)

    warning = check_gpu_probe(command)
    if warning:
        return warn(warning)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())