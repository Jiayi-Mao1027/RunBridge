#!/usr/bin/env python3
"""Stop hook – persist session checkpoint for durable state recovery.

When a Codex session ends (user quit, timeout, crash), this hook:
  - Snapshots current stage/phase/progress
  - Records owned-process state
  - Writes a checkpoint file that session_start_summary.py reads on resume
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


RUNTIME_STATE = Path.home() / '.codex' / 'runtime_state'
CHECKPOINT_PATH = RUNTIME_STATE / 'checkpoint.json'
OWNED_PATH = RUNTIME_STATE / 'process_guard' / 'owned.json'
EVENT_LOG = RUNTIME_STATE / 'event_log.jsonl'


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def read_text(path: Path) -> str:
    if not path.exists():
        return ''
    try:
        return path.read_text(encoding='utf-8').strip()
    except Exception:
        return ''


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def find_repo_root() -> Path | None:
    cwd = Path.cwd()
    for d in [cwd, *cwd.parents]:
        if (d / 'AGENTS.md').exists() or (d / 'specs').is_dir():
            return d
    return cwd


def infer_stage_from_specs(repo: Path) -> dict:
    """Best-effort stage inference from current_run.md content."""
    cr = read_text(repo / 'specs' / 'current_run.md')
    info: dict = {'stage': 'unknown', 'phase': 'unknown'}
    if not cr:
        return info
    # Try to find stage/phase markers
    for line in cr.splitlines():
        low = line.lower().strip()
        if low.startswith('stage:') or low.startswith('## stage'):
            info['stage'] = line.split(':', 1)[-1].strip() if ':' in line else low
        if low.startswith('phase:') or low.startswith('## phase'):
            info['phase'] = line.split(':', 1)[-1].strip() if ':' in line else low
    return info


def recent_events(n: int = 20) -> list[dict]:
    """Read the last N events from the event log."""
    if not EVENT_LOG.exists():
        return []
    try:
        lines = EVENT_LOG.read_text(encoding='utf-8').strip().splitlines()
        events = []
        for line in lines[-n:]:
            try:
                events.append(json.loads(line))
            except Exception:
                pass
        return events
    except Exception:
        return []


def count_owned_processes() -> dict:
    owned = read_json(OWNED_PATH, {'items': []})
    items = owned.get('items', [])
    return {
        'count': len(items),
        'pids': [item.get('pid') for item in items],
        'labels': [item.get('label', '') for item in items],
    }


def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        hook_input = {}

    repo = find_repo_root()
    stage_info = infer_stage_from_specs(repo) if repo else {}

    checkpoint = {
        'stopped_at': now_iso(),
        'stop_reason': hook_input.get('reason', 'unknown'),
        'repo_root': str(repo) if repo else None,
        'stage': stage_info.get('stage', 'unknown'),
        'phase': stage_info.get('phase', 'unknown'),
        'owned_processes': count_owned_processes(),
        'recent_events': recent_events(10),
        'pending_issues': [],  # Populated if we can read receipts
    }

    # Try to gather pending issues from the latest receipt
    if repo:
        runs_dir = repo / 'artifacts' / 'runs'
        if runs_dir.is_dir():
            run_dirs = sorted(
                [d for d in runs_dir.iterdir() if d.is_dir()],
                key=lambda d: d.stat().st_mtime,
                reverse=True,
            )
            if run_dirs:
                receipts_dir = run_dirs[0] / 'receipts'
                if receipts_dir.is_dir():
                    for rf in sorted(receipts_dir.iterdir(), reverse=True):
                        if rf.suffix == '.json':
                            receipt = read_json(rf)
                            if receipt and 'issues' in receipt:
                                checkpoint['pending_issues'] = receipt['issues']
                            break

    write_json(CHECKPOINT_PATH, checkpoint)

    # Also append a stop event to the log
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with EVENT_LOG.open('a', encoding='utf-8') as f:
        f.write(json.dumps({
            'type': 'session_stop',
            'timestamp': now_iso(),
            'reason': hook_input.get('reason', 'unknown'),
            'checkpoint_path': str(CHECKPOINT_PATH),
        }, ensure_ascii=False) + '\n')

    result = {
        'hook': 'stop_checkpoint',
        'checkpoint_path': str(CHECKPOINT_PATH),
        'stage': checkpoint['stage'],
        'owned_process_count': checkpoint['owned_processes']['count'],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
