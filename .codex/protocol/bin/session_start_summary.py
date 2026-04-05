#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

def resolve_codex_root() -> Path:
    return Path(__file__).resolve().parents[2]


OUT_DIR = resolve_codex_root() / "runtime_state"
OUT_FILE = OUT_DIR / "session_start_last.json"
CHECKPOINT_FILE = OUT_DIR / "checkpoint.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


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

    checkpoint = read_json(CHECKPOINT_FILE)
    if checkpoint:
        record["checkpoint"] = checkpoint

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
