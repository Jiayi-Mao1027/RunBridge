# Claude Control Hooks v2

These hooks adapt Claude Code hook payloads into the bridge-window workflow runtime.

Active path:

`Claude hook payload -> RuntimeEvent -> check_event -> update_runtime -> notify -> runtime_snapshot`

The hooks do not write authoritative state directly. They call:

`python ../.claude/control/runtime/main.py --event-json ... --persist`

Included hooks:

- `SessionStart`: creates a fresh run_id for the new Claude Code session and records it as the project's active run.
- `UserPromptSubmit`: marks frozen semantics as requiring refresh.
- `PreToolUse`: records bridge call intent/precheck/start for `call_bridge_sdk`, start events for bridge-leader lifecycle tools, and safe observer tool-start cards for every Claude Code session.
- `PostToolUse`: records bridge SDK failure/return, bridge-leader tool success/failure, and safe observer tool-completion cards for every Claude Code session.
- Bridge window open/accept/reject are workflow-internal runtime events emitted by `control/runtime/bridge_leader.py`, not Claude Code hook events.
- `TaskCreated`: records authoritative task identity after bridge-leader task creation.
- `TaskCompleted`: records completion-contract evidence.
- `TeammateIdle`: records long-running wait, timeout, or process-loss evidence.
- `Stop`: records a safe session stop event and blocks stop while hard-stop or approval state is unresolved.
- `SessionEnd`: records a safe session-end event and writes a lightweight session-end snapshot when bound to a run.

Observer side-channel:

- Run-bound tool/session records are written under the active run as `tool_events.jsonl`, `session_events.jsonl`, `session_bindings.jsonl`, and `active_operations.json`.
- Unbound or direct session records are written under `.claude/runtime_state/session_observer/` instead of being dropped.
- Tool records include session kind, binding state, session/run/window/team/task IDs when known, teammate/agent identity, tool name/id, status, safe input previews, file refs, bounded output tails, and tool-specific summaries.

The old task-action dispatcher is not the active hook path.
