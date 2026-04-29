# Claude Control Hooks v2

These hooks adapt Claude Code hook payloads into the bridge-window workflow runtime.

Active path:

`Claude hook payload -> RuntimeEvent -> check_event -> update_runtime -> notify -> runtime_snapshot`

The hooks do not write authoritative state directly. They call:

`python ../.claude/control/runtime/main.py --event-json ... --persist`

Included hooks:

- `SessionStart`: creates a fresh run_id for the new Claude Code session and records it as the project's active run.
- `UserPromptSubmit`: marks frozen semantics as requiring refresh.
- `PreToolUse`: records bridge call intent/precheck/start for `call_bridge_sdk`, and start events for bridge-leader lifecycle tools.
- `PostToolUse`: records bridge SDK failure/return and bridge-leader tool success/failure.
- Bridge window open/accept/reject are workflow-internal runtime events emitted by `control/runtime/bridge_leader.py`, not Claude Code hook events.
- `TaskCreated`: records authoritative task identity after bridge-leader task creation.
- `TaskCompleted`: records completion-contract evidence.
- `TeammateIdle`: records long-running wait, timeout, or process-loss evidence.
- `Stop`: blocks stop while hard-stop or approval state is unresolved.
- `SessionEnd`: writes a lightweight session-end snapshot only.

The old task-action dispatcher is not the active hook path.
