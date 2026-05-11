# Runtime v2

Active entry point:

`main.py -> workflow_runtime.dispatch_workflow_event`

The runtime accepts `RuntimeEvent` objects only. It does not accept legacy task/run action requests at `main.py`.

Bridge execution entry points:

- `main_leader.decide_next_bridge_packet`: reads current runtime truth and builds one packet for exactly one bridge invocation window.
- `bridge_sdk.call_bridge_sdk`: SDK/tool-facing call that records the main bridge lifecycle and invokes bridge-leader execution.
- `bridge_leader.execute_bridge_window`: bridge-owned team/task/message/completion/delete execution layer for one packet.
- `bridge/executors/*`: shared `BridgeExecutor` boundary. `cli` wraps the current Claude CLI path, `simulate` wraps smoke execution, and `sdk` is the explicit SDK-in-SDK migration skeleton.
- `workflow_runtime.reconcile_workflow_from_ledger`: replays `event_log.jsonl` and rebuilds derived run ledger, transitions, and snapshot.
- `../mcp/bridge_server.py`: parent-level MCP server used by repos launched under the same workspace parent. In Claude Code these tools appear as `mcp__bridge__read_runtime_snapshot`, `mcp__bridge__build_bridge_packet`, `mcp__bridge__call_bridge_sdk`, `mcp__bridge__dispatch_workflow_event`, and `mcp__bridge__reconcile_workflow_from_ledger`.

Canonical boundaries:

- `runtime_event_envelope.py` normalizes CLI stream records, hook observer records, runtime transitions, and future SDK stream records into `runtime_event_envelope.v1`.
- `artifact_refs.py` normalizes and validates `artifact_ref.v1`, including producer event/agent, run/window/team/task binding, optional path, and sha256.
- `completion_validator.py` combines schema, contract, artifact, semantic coverage, lifecycle, and failure-disposition checks before a bridge result can satisfy completion. Completion projection records use the validator output so Companion shows the same structured checks the runtime used.
- `policy_compiler.py` is the compatibility compiler for policy JSON and schemas; Python should execute compiled policy instead of growing new scattered hardcodes. It now emits `validation_results` for policy schema checks and team-planner phase-rule references.
- `team_planner.py` contains the policy-owned risk-based selector for reducing redundant teammate specs without changing the one-window/one-team/one-task invariant. The selected team and full decision are recorded in `team_planning` on each packet.

Durability and recovery pieces:

- `state_graph.py` + `../policy/state_graph.json`: native RunBridge graph replay keyed by current graph node and lifecycle event.
- `checkpoint_store.py`: per-event checkpoints written after the current event is persisted and replayable.
- `retry_policy.py`: Temporal-style policy, attempt, backoff, non-retryable, and exhaustion decisions.
- `retry_driver.py`: disabled Beta2 driver contract for reading `retry_attempt_scheduled` events, checking delay/same-packet/max-attempt gates, and mapping recovery actions.
- `output_guardrails.py`: structured output validation for packets, bridge results, teammate reports, completion reports, and log manifests. The local schema checker is intentionally minimal, not a full JSON Schema implementation.
- `trajectory.py`: UI-safe timeline plus evidence links from completion, artifact, process, guardrail, and retry events.

SDK stream observability:

- `claude_cli_executor.py` invokes Claude CLI print mode with `--output-format stream-json --verbose --include-partial-messages`.
- SDK stream records keep bounded `message_preview`, `text_delta`, `input_json_delta`, compact tool metadata, and `raw_stream_event_type`.
- SDK stream `tool_use`/`tool_result` records are not real hook tool events. Real tool events come from `.claude/hooks` into run-scoped observer JSONL.

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
