#!/usr/bin/env python3
from __future__ import annotations

import re
import shlex

from common import (
    GPU_PROBED_FLAG,
    OWNED_PATH,
    deny_pre_tool,
    parse_command,
    pid_ppid_map,
    proc_cmdline,
    read_json,
    system_message,
    load_hook_input,
)


MANAGED_ANCESTOR_MARKERS = (
    "/.claude-isolated/",
    "/.claude/hooks/",
)

DESTRUCTIVE_RE = re.compile(
    r"\brm\s+.*-[^\s]*r[^\s]*f|"
    r"\brm\s+-rf\b|"
    r"\bmkfs\b|"
    r"\bdd\s+.*of=/",
    re.IGNORECASE,
)

CRITICAL_PATHS = (
    "CLAUDE.md",
    ".claude-isolated/",
    ".claude/",
    "/etc/",
    "/usr/",
    "/home/",
)

GPU_LAUNCH_RE = re.compile(
    r"\btorchrun\b|"
    r"\bpython[^\n]*train\b|"
    r"\baccelerate\s+launch\b|"
    r"\bdeepspeed\b|"
    r"\bnvidia-smi\b|"
    r"\bCUDA_VISIBLE_DEVICES\b",
    re.IGNORECASE,
)


def live_owned_records() -> list[dict]:
    payload = read_json(OWNED_PATH, {"items": []}) or {"items": []}
    mapping = pid_ppid_map()
    live_pids = set(mapping)
    records: list[dict] = []
    for item in payload.get("items", []):
        pid_raw = item.get("pid")
        if not str(pid_raw).isdigit():
            continue
        pid = int(pid_raw)
        if pid not in live_pids:
            continue
        current_cmd = proc_cmdline(pid)
        saved_cmd = str(item.get("cmdline", "")).strip()
        if saved_cmd and current_cmd and current_cmd != saved_cmd:
            continue
        records.append(item)
    return records


def effective_owned_pids() -> set[int]:
    records = live_owned_records()
    explicit = {
        int(item["pid"])
        for item in records
        if str(item.get("pid", "")).isdigit()
    }
    if not explicit:
        return set()

    mapping = pid_ppid_map()
    descendants = set(explicit)
    frontier = set(explicit)
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
            cmd = proc_cmdline(current)
            if any(marker in cmd for marker in MANAGED_ANCESTOR_MARKERS):
                ancestors.add(current)
            current = mapping.get(current)

    return descendants | ancestors


def check_kill_safety(command: str) -> str | None:
    try:
        argv = shlex.split(command)
    except Exception:
        argv = command.split()
    if not argv:
        return None

    tool = argv[0].lower()
    if tool not in {"kill", "pkill", "killall", "skill"}:
        return None

    if tool in {"pkill", "killall", "skill"}:
        return (
            f'Blocked: pattern-based process termination via "{tool}" is not allowed by default. '
            "Use explicit numeric PIDs from the current owned stack only."
        )

    pids_in_cmd: set[int] = set()
    for arg in argv[1:]:
        if arg.startswith("-"):
            continue
        if arg.isdigit():
            pids_in_cmd.add(int(arg))

    if not pids_in_cmd:
        return "Blocked: kill command must use explicit numeric PIDs from the current owned stack."

    owned = effective_owned_pids()
    foreign = pids_in_cmd - owned
    if foreign:
        return (
            f"Blocked: kill targets foreign PIDs {sorted(foreign)}. "
            f'Only owned PIDs may be killed. Currently owned: {sorted(owned) if owned else "none"}.'
        )
    return None


def check_destructive(command: str) -> str | None:
    if not DESTRUCTIVE_RE.search(command):
        return None
    for critical in CRITICAL_PATHS:
        if critical in command:
            return (
                f'Blocked: destructive operation targets critical path "{critical}". '
                "This requires explicit user authorization."
            )
    return None


def check_gpu_probe(command: str) -> str | None:
    if not GPU_LAUNCH_RE.search(command):
        return None
    if command.strip().startswith("nvidia-smi"):
        return None
    if not GPU_PROBED_FLAG.exists():
        return (
            "GPU launch detected but no GPU probe has been recorded in this session. "
            "Run nvidia-smi first."
        )
    return None


def main() -> int:
    payload = load_hook_input()
    command = parse_command(payload)
    if not command:
        return 0

    reason = check_kill_safety(command)
    if reason:
        return deny_pre_tool(reason)

    reason = check_destructive(command)
    if reason:
        return deny_pre_tool(reason)

    warning = check_gpu_probe(command)
    if warning:
        return system_message(warning)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
