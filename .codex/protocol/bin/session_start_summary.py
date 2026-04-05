#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


OUT_DIR = Path.home() / ".codex" / "runtime_state"
OUT_FILE = OUT_DIR / "session_start_last.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    raw = sys.stdin.read()
    payload = {}

    if raw.strip():
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw_stdin": raw}

    record = {
        "written_at": now(),
        "hook_payload": payload,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    # 给 hook UI 一个简短输出，但不要阻断
    print(str(OUT_FILE))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        # SessionStart 不应阻断主流程
        print(f"[session_start_summary] non-fatal error: {e}", file=sys.stderr)
        raise SystemExit(0)