#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from datetime import datetime, timezone

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


def owned_pid_set() -> set[int]:
    payload = read_json(OWNED, {'items': []})
    return {
        int(item['pid'])
        for item in payload.get('items', [])
        if str(item.get('pid', '')).isdigit()
    }


def cmd_snapshot() -> int:
    write_json(BASELINE, {'written_at': now(), 'pids': list_pids()})
    print(str(BASELINE))
    return 0


def cmd_register(pid: int, label: str) -> int:
    payload = read_json(OWNED, {'items': []})
    items = payload.get('items', [])

    # 去重，避免重复注册同一个 pid
    items = [x for x in items if int(x.get('pid', -1)) != pid]
    items.append({
        'pid': pid,
        'label': label,
        'registered_at': now(),
        'ppid': os.getppid(),
    })

    payload['items'] = items
    write_json(OWNED, payload)
    print(str(OWNED))
    return 0


def cmd_list() -> int:
    payload = read_json(OWNED, {'items': []})
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_check(pid: int) -> int:
    if pid in owned_pid_set():
        print('owned')
    else:
        print('foreign')
    return 0


def cmd_assert_owned(pid: int) -> int:
    if pid in owned_pid_set():
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

    p_chk = sub.add_parser('check')
    p_chk.add_argument('pid', type=int)

    p_assert = sub.add_parser('assert-owned')
    p_assert.add_argument('pid', type=int)

    sub.add_parser('list')

    args = ap.parse_args()

    if args.cmd == 'snapshot':
        return cmd_snapshot()
    if args.cmd == 'register':
        return cmd_register(args.pid, args.label)
    if args.cmd == 'check':
        return cmd_check(args.pid)
    if args.cmd == 'assert-owned':
        return cmd_assert_owned(args.pid)
    if args.cmd == 'list':
        return cmd_list()
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
