# Runtime v2

Active entry point:

`main.py -> workflow_runtime.dispatch_workflow_event`

The runtime accepts `RuntimeEvent` objects only. It does not accept legacy task/run action requests at `main.py`.

Bridge execution entry points:

- `main_leader.decide_next_bridge_packet`: reads current runtime truth and builds one packet for exactly one bridge invocation window.
- `bridge_sdk.call_bridge_sdk`: SDK/tool-facing call that records the main bridge lifecycle and invokes bridge-leader execution.
- `bridge_leader.execute_bridge_window`: bridge-owned team/task/message/completion/delete execution layer for one packet.
- `workflow_runtime.reconcile_workflow_from_ledger`: replays `event_log.jsonl` and rebuilds derived run ledger, transitions, and snapshot.
- `../mcp/bridge_server.py`: parent-level MCP server used by repos launched under the same workspace parent. In Claude Code these tools appear as `mcp__bridge__read_runtime_snapshot`, `mcp__bridge__build_bridge_packet`, `mcp__bridge__call_bridge_sdk`, `mcp__bridge__dispatch_workflow_event`, and `mcp__bridge__reconcile_workflow_from_ledger`.

CLI examples:

- Build a packet from a repo cwd: `python ../.claude/control/runtime/main.py --control-root ../.claude/control --run-id RUN --build-bridge-packet --user-instruction "..."`
- Call bridge SDK from a repo cwd: `python ../.claude/control/runtime/main.py --control-root ../.claude/control --packet-file packet.json --call-bridge-sdk --persist`
- Reconcile from event ledger from a repo cwd: `python ../.claude/control/runtime/main.py --control-root ../.claude/control --run-id RUN --reconcile-from-ledger --persist`

Active ledgers per run:

- `run_ledger.json`
- `runtime_snapshot.json`
- `event_log.jsonl`
- `check_ledger.jsonl`
- `update_ledger.jsonl`
- `transitions.jsonl`
- `main_leader_inbox.jsonl`

The older action-dispatch modules in this directory are retained only as historical implementation material. They are not the active execution path for the bridge-window workflow.
