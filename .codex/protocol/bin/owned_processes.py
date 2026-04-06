#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from datetime import datetime, timezone


MANAGED_ANCESTOR_MARKERS = (
    '/.agents/skills/cc-',
    'claude_skill_runner.py',
)

def resolve_codex_root() -> Path:
    return Path(__file__).resolve().parents[2]


STATE_DIR = resolve_codex_root() / 'runtime_state' / 'process_guard'
BASELINE = STATE_DIR / 'baseline.json'
OWNED = STATE_DIR / 'owned.json'


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_pids() -> list[int]:
    out = subprocess.check_output(['ps', '-e', '-o', 'pid='], text=True)
    return sorted({int(x.strip()) for x in out.splitlines() if x.strip().isdigit()})


def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def read_owned_payload() -> dict:
    return read_json(OWNED, {'items': []})


def owned_pid_set() -> set[int]:
    payload = read_owned_payload()
    return {
        int(item['pid'])
        for item in payload.get('items', [])
        if str(item.get('pid', '')).isdigit()
    }


def wrapper_pid_set() -> set[int]:
    payload = read_owned_payload()
    return {
        int(item['wrapper_pid'])
        for item in payload.get('items', [])
        if str(item.get('wrapper_pid', '')).isdigit()
    }


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


def live_owned_records() -> list[dict]:
    payload = read_owned_payload()
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


def is_managed_ancestor_pid(pid: int) -> bool:
    cmd = proc_cmdline(pid)
    return any(marker in cmd for marker in MANAGED_ANCESTOR_MARKERS)


def effective_owned_pid_set() -> set[int]:
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
            if is_managed_ancestor_pid(current):
                ancestors.add(current)
            current = mapping.get(current)

    return descendants | ancestors


def cmd_snapshot() -> int:
    write_json(BASELINE, {'written_at': now(), 'pids': list_pids()})
    print(str(BASELINE))
    return 0


def cmd_register(pid: int, label: str, wrapper_pid: int | None = None) -> int:
    payload = read_owned_payload()
    items = payload.get('items', [])

    # 去重，避免重复注册同一个 pid
    items = [x for x in items if int(x.get('pid', -1)) != pid]
    item = {
        'pid': pid,
        'label': label,
        'registered_at': now(),
        'ppid': os.getppid(),
        'wrapper_pid': wrapper_pid,
        'cmdline': proc_cmdline(pid),
    }
    if wrapper_pid is not None:
        item['wrapper_cmdline'] = proc_cmdline(wrapper_pid)

    items.append({
        **item,
    })

    payload['items'] = items
    write_json(OWNED, payload)
    print(str(OWNED))
    return 0


def cmd_list() -> int:
    payload = read_json(OWNED, {'items': []})
    payload['effective_pids'] = sorted(effective_owned_pid_set())
    payload['live_items'] = live_owned_records()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_check(pid: int) -> int:
    if pid in effective_owned_pid_set():
        print('owned')
    else:
        print('foreign')
    return 0


def cmd_assert_owned(pid: int) -> int:
    if pid in effective_owned_pid_set():
        print('owned')
        return 0
    print('foreign')
    return 2


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('snapshot')

    p_reg = sub.add_parser('register')
    p_reg.add_argument('pid', type=int)
    p_reg.add_argument('--label', default='')
    p_reg.add_argument('--wrapper-pid', type=int)

    p_chk = sub.add_parser('check')
    p_chk.add_argument('pid', type=int)

    p_assert = sub.add_parser('assert-owned')
    p_assert.add_argument('pid', type=int)

    sub.add_parser('list')

    args = ap.parse_args()

    if args.cmd == 'snapshot':
        return cmd_snapshot()
    if args.cmd == 'register':
        return cmd_register(args.pid, args.label, args.wrapper_pid)
    if args.cmd == 'check':
        return cmd_check(args.pid)
    if args.cmd == 'assert-owned':
        return cmd_assert_owned(args.pid)
    if args.cmd == 'list':
        return cmd_list()
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
