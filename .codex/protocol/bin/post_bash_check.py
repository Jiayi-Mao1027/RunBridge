#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

def resolve_codex_root() -> Path:
    return Path(__file__).resolve().parents[2]


RUNTIME_STATE = resolve_codex_root() / 'runtime_state'
OWNED_PATH = RUNTIME_STATE / 'process_guard' / 'owned.json'
GPU_PROBED_FLAG = RUNTIME_STATE / 'gpu_probed'
EVENT_LOG = RUNTIME_STATE / 'event_log.jsonl'

BG_PID_RE = re.compile(
    r'\[\d+\]\s+(\d{3,})|'
    r'\bPID[:\s]+(\d{3,})|'
    r'\bstarted\s+.*?(\d{4,})',
    re.IGNORECASE,
)

BG_LAUNCH_RE = re.compile(
    r'\bnohup\b|'
    r'&\s*$|'
    r'\btorchrun\b|'
    r'\baccelerate\s+launch\b|'
    r'\bdeepspeed\b',
    re.IGNORECASE,
)

GPU_PROBE_RE = re.compile(
    r'\bgpu_probe\.py\b|'
    r'\bnvidia-smi\b',
    re.IGNORECASE,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def append_event(event: dict) -> None:
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with EVENT_LOG.open('a', encoding='utf-8') as f:
        f.write(json.dumps(event, ensure_ascii=False) + '\n')


def extract_bg_pids(output: str) -> list[int]:
    pids = []
    for m in BG_PID_RE.finditer(output):
        for g in m.groups():
            if g and g.isdigit():
                pid = int(g)
                if 2 <= pid <= 4194304:
                    pids.append(pid)
    return pids


def register_pids(pids: list[int], label: str) -> int:
    payload = read_json(OWNED_PATH, {'items': []})
    existing = {
        int(item['pid'])
        for item in payload.get('items', [])
        if str(item.get('pid', '')).isdigit()
    }
    added = 0
    for pid in pids:
        if pid not in existing:
            payload['items'].append({
                'pid': pid,
                'label': label,
                'registered_at': now_iso(),
                'ppid': os.getppid(),
                'source': 'post_bash_hook_auto',
            })
            added += 1
    if added:
        write_json(OWNED_PATH, payload)
    return added


def parse_tool_response(tool_response):
    """
    Codex docs say PostToolUse provides tool_response.
    In practice this may be a JSON value, often a JSON string.
    """
    if isinstance(tool_response, dict):
        stdout_text = tool_response.get('stdout', '') or tool_response.get('output', '')
        exit_code = tool_response.get('exit_code', tool_response.get('exitCode', 0))
        return str(stdout_text or ''), exit_code

    if isinstance(tool_response, str):
        raw = tool_response.strip()
        if not raw:
            return '', 0
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                stdout_text = parsed.get('stdout', '') or parsed.get('output', '')
                exit_code = parsed.get('exit_code', parsed.get('exitCode', 0))
                return str(stdout_text or ''), exit_code
        except Exception:
            pass
        return raw, 0

    return str(tool_response or ''), 0


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

    stdout_text, exit_code = parse_tool_response(hook_input.get('tool_response'))

    if command and GPU_PROBE_RE.search(command):
        GPU_PROBED_FLAG.parent.mkdir(parents=True, exist_ok=True)
        GPU_PROBED_FLAG.write_text(now_iso(), encoding='utf-8')

    if command and BG_LAUNCH_RE.search(command):
        pids = extract_bg_pids(stdout_text)
        if pids:
            register_pids(pids, label=f'auto:{command[:60]}')

    try:
        exit_code_int = int(exit_code)
    except (TypeError, ValueError):
        exit_code_int = 0

    if exit_code_int != 0:
        append_event({
            'type': 'bash_nonzero_exit',
            'timestamp': now_iso(),
            'command_preview': command[:200] if command else '',
            'exit_code': exit_code_int,
        })

    # Default success path: no output, exit 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
