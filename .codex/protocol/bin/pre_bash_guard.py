#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

def resolve_codex_root() -> Path:
    return Path(__file__).resolve().parents[2]


RUNTIME_STATE = resolve_codex_root() / 'runtime_state'
OWNED_PATH = RUNTIME_STATE / 'process_guard' / 'owned.json'
GPU_PROBED_FLAG = RUNTIME_STATE / 'gpu_probed'
MANAGED_ANCESTOR_MARKERS = (
    '/.agents/skills/cc-',
    'claude_skill_runner.py',
)

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


def live_owned_records() -> list[dict]:
    payload = read_json(OWNED_PATH, {'items': []})
    mapping = pid_ppid_map()
    live_pids = set(mapping)
    records: list[dict] = []
    for item in payload.get('items', []):
        pid_raw = item.get('pid')
        if not str(pid_raw).isdigit():
            continue
        pid = int(pid_raw)
        if pid not in live_pids:
            continue
        current_cmd = proc_cmdline(pid)
        saved_cmd = str(item.get('cmdline', '')).strip()
        if saved_cmd and current_cmd and current_cmd != saved_cmd:
            continue

        wrapper_raw = item.get('wrapper_pid')
        if str(wrapper_raw).isdigit():
            wrapper_pid = int(wrapper_raw)
            if wrapper_pid not in live_pids:
                item = dict(item)
                item.pop('wrapper_pid', None)
                item.pop('wrapper_cmdline', None)
            else:
                wrapper_cmd = proc_cmdline(wrapper_pid)
                saved_wrapper_cmd = str(item.get('wrapper_cmdline', '')).strip()
                if saved_wrapper_cmd and wrapper_cmd and wrapper_cmd != saved_wrapper_cmd:
                    item = dict(item)
                    item.pop('wrapper_pid', None)
                    item.pop('wrapper_cmdline', None)

        records.append(item)
    return records


def pid_ppid_map() -> dict[int, int]:
    out = subprocess.check_output(['ps', '-e', '-o', 'pid=,ppid='], text=True)
    mapping: dict[int, int] = {}
    for raw in out.splitlines():
        parts = raw.strip().split()
        if len(parts) != 2:
            continue
        pid_s, ppid_s = parts
        if pid_s.isdigit() and ppid_s.isdigit():
            mapping[int(pid_s)] = int(ppid_s)
    return mapping


def proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f'/proc/{pid}/cmdline').read_bytes()
    except Exception:
        return ''
    return raw.replace(b'\x00', b' ').decode('utf-8', errors='replace').strip()


def effective_owned_pids() -> set[int]:
    records = live_owned_records()
    explicit = {
        int(item['pid'])
        for item in records
        if str(item.get('pid', '')).isdigit()
    }
    if not explicit:
        return set()

    mapping = pid_ppid_map()
    wrapper_pids = {
        int(item['wrapper_pid'])
        for item in records
        if str(item.get('wrapper_pid', '')).isdigit()
    }
    descendants = set(explicit) | wrapper_pids
    frontier = set(explicit) | wrapper_pids
    while frontier:
        next_frontier: set[int] = set()
        for pid, ppid in mapping.items():
            if ppid in frontier and pid not in descendants:
                descendants.add(pid)
                next_frontier.add(pid)
        frontier = next_frontier

    ancestors: set[int] = set()
    for pid in explicit:
        current = mapping.get(pid)
        while current and current > 1 and current not in ancestors:
            cmd = proc_cmdline(current)
            if any(marker in cmd for marker in MANAGED_ANCESTOR_MARKERS):
                ancestors.add(current)
            current = mapping.get(current)

    return descendants | ancestors


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
    try:
        argv = shlex.split(command)
    except Exception:
        argv = command.split()
    if not argv:
        return None

    tool_index = 0
    while tool_index < len(argv) and '=' in argv[tool_index] and not argv[tool_index].startswith('-'):
        name, _, value = argv[tool_index].partition('=')
        if name and value and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            tool_index += 1
            continue
        break

    if tool_index >= len(argv):
        return None

    tool = argv[tool_index].lower()
    if tool not in {'kill', 'pkill', 'killall', 'skill'}:
        return None

    if tool in {'pkill', 'killall', 'skill'}:
        return (
            f'Blocked: pattern-based process termination via "{tool}" is not allowed by default. '
            'Use explicit numeric PIDs from the current owned stack only.'
        )

    pids_in_cmd: set[int] = set()
    for arg in argv[tool_index + 1:]:
        if arg.startswith('-'):
            continue
        if arg.isdigit():
            pids_in_cmd.add(int(arg))

    if not pids_in_cmd:
        return 'Blocked: kill command must use explicit numeric PIDs from the current owned stack.'

    owned = effective_owned_pids()
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
