#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('path')
    args = ap.parse_args()
    path = Path(args.path)
    if not path.exists():
        print(f'file not found: {path}', file=sys.stderr)
        return 1
    try:
        json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as e:
        print(f'invalid json: {path}: {e}', file=sys.stderr)
        return 1
    print(f'valid json: {path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
