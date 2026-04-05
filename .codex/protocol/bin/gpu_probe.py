#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from dataclasses import dataclass, asdict

@dataclass
class GPU:
    index: int
    name: str
    memory_total_mib: int
    memory_used_mib: int
    util_gpu: int | None

    @property
    def free_mib(self) -> int:
        return self.memory_total_mib - self.memory_used_mib

    @property
    def is_h100(self) -> bool:
        return 'H100' in self.name.upper()


def parse_int(v: str) -> int | None:
    v = (v or '').strip().replace('%', '')
    if not v or v in {'[Not Supported]', 'N/A'}:
        return None
    try:
        return int(float(v))
    except Exception:
        return None


def query() -> list[GPU]:
    cmd = [
        'nvidia-smi',
        '--query-gpu=index,name,memory.total,memory.used,utilization.gpu',
        '--format=csv,noheader,nounits',
    ]
    out = subprocess.check_output(cmd, text=True)
    rows = csv.reader([line for line in out.splitlines() if line.strip()])
    gpus = []
    for row in rows:
        if len(row) < 5:
            continue
        idx, name, mem_total, mem_used, util = [x.strip() for x in row]
        gpus.append(GPU(
            index=int(idx),
            name=name,
            memory_total_mib=int(float(mem_total)),
            memory_used_mib=int(float(mem_used)),
            util_gpu=parse_int(util),
        ))
    return gpus


def choose(gpus: list[GPU], requested: int, max_gpus: int) -> dict:
    requested = max(1, min(requested, max_gpus))
    ranked = sorted(
        gpus,
        key=lambda g: (
            0 if g.is_h100 else 1,
            -(g.free_mib),
            (g.util_gpu if g.util_gpu is not None else 9999),
            g.index,
        ),
    )
    selected = ranked[:requested]
    return {
        'requested_gpu_count': requested,
        'selected_gpu_ids': [g.index for g in selected],
        'selected_gpu_models': [g.name for g in selected],
        'all_gpus': [asdict(g) | {'free_mib': g.free_mib, 'is_h100': g.is_h100} for g in ranked],
        'prefer_h100': True,
        'fallback_reason': '' if all(g.is_h100 for g in selected) else 'Preferred H100 set unavailable or insufficient.',
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--requested', type=int, default=2)
    ap.add_argument('--max-gpus', type=int, default=3)
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    try:
        gpus = query()
    except Exception as e:
        print(json.dumps({'error': str(e)}))
        return 2
    payload = choose(gpus, args.requested, args.max_gpus)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print('Requested:', payload['requested_gpu_count'])
        print('Selected IDs:', payload['selected_gpu_ids'])
        print('Selected Models:', payload['selected_gpu_models'])
        if payload['fallback_reason']:
            print('Fallback:', payload['fallback_reason'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
