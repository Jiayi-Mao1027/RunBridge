#!/usr/bin/env python3
from __future__ import annotations

import sys

from common import CHECKPOINT_PATH, SESSION_START_PATH, load_hook_input, now_iso, read_json, write_json


def main() -> int:
    payload = load_hook_input()
    record = {
        "written_at": now_iso(),
        "hook_payload": payload,
    }
    checkpoint = read_json(CHECKPOINT_PATH, {})
    if checkpoint:
        record["checkpoint"] = checkpoint
    write_json(SESSION_START_PATH, record)
    print(str(SESSION_START_PATH))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[session_start_summary] non-fatal error: {exc}", file=sys.stderr)
        raise SystemExit(0)
