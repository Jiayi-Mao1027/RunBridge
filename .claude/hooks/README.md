# Claude Control Hooks v2

These hooks adapt Claude Code hook payloads into the bridge-window workflow runtime.

Active path:

`Claude hook payload -> RuntimeEvent -> check_event -> update_runtime -> notify -> runtime_snapshot`

The hooks do not write authoritative state directly. They call:

`python .claude/control/runtime/main.py --event-json ... --persist`

Included hooks:

- `UserPromptSubmit`: marks frozen semantics as requiring refresh.
- `PreToolUse`: records bridge call intent/precheck/start for `call_bridge_sdk`, and start events for bridge-leader lifecycle tools.
- `PostToolUse`: records bridge SDK failure/return and bridge-leader tool success/failure.
- `BridgeWindowOpened`, `BridgePacketAccepted`, `BridgePacketRejected`: bridge-side events for packet/window acceptance before team/task work begins.
- `TaskCreated`: records authoritative task identity after bridge-leader task creation.
- `TaskCompleted`: records completion-contract evidence.
- `TeamIdle`: records long-running wait, timeout, or process-loss evidence.
- `Stop`: blocks stop while hard-stop or approval state is unresolved.
- `SessionEnd`: writes a lightweight session-end snapshot only.

The old task-action dispatcher is not the active hook path.
