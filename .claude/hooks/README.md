# Claude Control Hooks v1

This package provides minimal hook adapters for a task-centric control runtime.

Included hooks:
- TaskCreated
- TaskCompleted
- PreToolUse
- Stop
- SessionEnd

Design:
- Hooks do not write authoritative state directly.
- Hooks call the runtime CLI (`main.py`) where appropriate.
- `PreToolUse` only gates tools.
- `SessionEnd` only writes a lightweight snapshot.

Expected layout on your machine:
- `.claude/control/runtime/main.py`
- `.claude/control/runtime_state/runs/...`
- `.claude/hooks/...`

These hook adapters assume they live under `.claude/hooks/` and the runtime lives under
`.claude/control/runtime/`.
