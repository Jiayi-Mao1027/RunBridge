#!/usr/bin/env python3
from __future__ import annotations

import os
import re

from common import (
    EVENT_LOG,
    GPU_PROBED_FLAG,
    OWNED_PATH,
    append_jsonl,
    load_hook_input,
    now_iso,
    parse_command,
    parse_tool_response,
    read_json,
    write_json,
)


BG_PID_RE = re.compile(
    r"\[\d+\]\s+(\d{3,})|"
    r"\bPID[:\s]+(\d{3,})|"
    r"\bstarted\s+.*?(\d{4,})",
    re.IGNORECASE,
)

BG_LAUNCH_RE = re.compile(
    r"\bnohup\b|"
    r"&\s*$|"
    r"\btorchrun\b|"
    r"\baccelerate\s+launch\b|"
    r"\bdeepspeed\b",
    re.IGNORECASE,
)

GPU_PROBE_RE = re.compile(
    r"\bnvidia-smi\b|"
    r"\bgpu_probe\.py\b",
    re.IGNORECASE,
)


def extract_bg_pids(output: str) -> list[int]:
    pids: list[int] = []
    for match in BG_PID_RE.finditer(output):
        for group in match.groups():
            if group and group.isdigit():
                pid = int(group)
                if 2 <= pid <= 4194304:
                    pids.append(pid)
    return pids


def register_pids(pids: list[int], label: str) -> int:
    payload = read_json(OWNED_PATH, {"items": []}) or {"items": []}
    existing = {
        int(item["pid"])
        for item in payload.get("items", [])
        if str(item.get("pid", "")).isdigit()
    }
    added = 0
    for pid in pids:
        if pid in existing:
            continue
        payload["items"].append(
            {
                "pid": pid,
                "label": label,
                "registered_at": now_iso(),
                "ppid": os.getppid(),
                "source": "post_bash_hook_auto",
            }
        )
        added += 1
    if added:
        write_json(OWNED_PATH, payload)
    return added


def main() -> int:
    payload = load_hook_input()
    command = parse_command(payload)
    stdout_text, exit_code = parse_tool_response(payload)

    if command and GPU_PROBE_RE.search(command):
        GPU_PROBED_FLAG.parent.mkdir(parents=True, exist_ok=True)
        GPU_PROBED_FLAG.write_text(now_iso(), encoding="utf-8")

    if command and BG_LAUNCH_RE.search(command):
        pids = extract_bg_pids(stdout_text)
        if pids:
            register_pids(pids, label=f"auto:{command[:60]}")

    if exit_code != 0:
        append_jsonl(
            EVENT_LOG,
            {
                "type": "bash_nonzero_exit",
                "timestamp": now_iso(),
                "command_preview": command[:200],
                "exit_code": exit_code,
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
