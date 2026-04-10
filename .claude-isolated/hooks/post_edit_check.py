#!/usr/bin/env python3
from __future__ import annotations

import json

from common import TOUCHED_FILES_LOG, append_jsonl, load_hook_input, now_iso, project_root, relative_to_project, system_message, tool_paths


def main() -> int:
    payload = load_hook_input()
    root = project_root(payload)
    touched = [relative_to_project(path, root) for path in tool_paths(payload)]
    for relpath in touched:
        append_jsonl(
            TOUCHED_FILES_LOG,
            {
                "timestamp": now_iso(),
                "project_root": str(root),
                "path": relpath,
            },
        )

    for path in tool_paths(payload):
        if path.suffix != ".json" or not path.exists():
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return system_message(f"Edited JSON file failed to parse: {relative_to_project(path, root)} ({exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
