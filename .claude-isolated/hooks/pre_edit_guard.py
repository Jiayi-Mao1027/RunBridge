#!/usr/bin/env python3
from __future__ import annotations

from common import CONFIG_ROOT, deny_pre_tool, load_hook_input, project_root, tool_paths


PROTECTED_SEGMENTS = (
    "/.git/",
    "/.claude-isolated/runtime_state/",
    "/etc/",
    "/usr/",
)


def main() -> int:
    payload = load_hook_input()
    root = project_root(payload)
    for path in tool_paths(payload):
        path_str = str(path)
        if any(segment in path_str for segment in PROTECTED_SEGMENTS):
            return deny_pre_tool(f"Blocked: edit target is protected: {path_str}")
        if path.is_absolute() and root not in path.parents and path != root and CONFIG_ROOT not in path.parents:
            return deny_pre_tool(f"Blocked: edit target is outside the active project root: {path_str}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
