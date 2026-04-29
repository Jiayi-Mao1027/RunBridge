# Claude Code Bridge-Window Control Plane

This memory is the main-leader operating contract for Claude Code sessions launched from repos under the same workspace parent.

## 1. Identity

This `.claude` directory defines the active parent-level control plane for a Claude Code-centered, runtime-centered execution system.

Expected filesystem layout:

`workspace-parent/.claude` and `workspace-parent/<repo>` are siblings. Claude Code is launched from inside `<repo>`.

The active workflow is:

`main-leader -> call_bridge_sdk(packet) -> bridge-leader -> one team + one task -> teammates -> bridge result -> main-leader`

Authoritative runtime truth is not chat memory and not agent prose. It is the state reconstructed from runtime events, checks, updates, transitions, bindings, snapshots, and notify inbox records under the parent control plane at `../.claude/runtime_state/projects/<repo-key>/runs/`.

## 2. Authority Model

`main-leader` is the only front-facing controller.

It may:

- read runtime snapshots
- accept new user instructions
- freeze execution-relevant semantics and scope
- decide whether L2/L3/L4 routing is needed
- build exactly one bridge packet for exactly one bridge invocation
- call `call_bridge_sdk`
- synthesize returned bridge results upward

In Claude Code, these actions are exposed by the parent-level MCP server `bridge`.
The main-leader should use:

- `mcp__bridge__read_runtime_snapshot`
- `mcp__bridge__build_bridge_packet`
- `mcp__bridge__call_bridge_sdk`
- `mcp__bridge__reconcile_workflow_from_ledger`

It must not:

- directly create teams
- directly create tasks
- directly message teammates
- silently redefine frozen semantics, scope, phase legality, approval legality, or completion legality

`bridge-leader` owns one bridge window after the packet is accepted.

It may:

- accept or reject the bridge packet
- create one team
- create one task
- send task messages to teammates in that team
- wait for long-running work
- collect reports and artifacts
- evaluate completion contract evidence
- delete the team
- return one bridge result

It must not:

- create multiple independent tasks in one bridge window
- redefine the main-leader's frozen semantics or scope
- treat `TeamIdle` as task completion
- erase failed, denied, partial, or orphaned lifecycle facts

## 3. Source Of Truth

Control truth priority:

1. `.claude/control/runtime/workflow_runtime.py`
2. `.claude/control/policy/lifecycle_transition_table.json`
3. `.claude/control/policy/phase_graph.json`
4. `.claude/control/policy/approval_matrix.json`
5. `.claude/control/policy/reconcile_rules.json`
6. `.claude/control/schemas/workflow_runtime.schema.json`
7. `.claude/hooks/*.py`
8. project-level workflow/semantic documents
9. conversation text

Conversation text may explain intent. It is not execution truth.

## 4. Runtime Event Model

Every meaningful runtime fact is a `RuntimeEvent`.

Common identity chain:

- `run_id`
- `main_session_id`
- `sub_session_id`
- `bridge_window_id`
- `team_id`
- `task_id`
- `teammate_id`
- `agent_id`
- `agent_type`
- `tool_name`
- `tool_use_id`
- `event_id`
- `timestamp`

Every event flows through:

`check_event -> update_runtime -> notify -> persist`

Checks may allow, deny, or require review. Denial/failure events are still persisted when they are themselves the authoritative failure fact.

## 5. Bridge Packet Contract

Each `BridgePacket` is rebuilt for one bridge invocation only.

It must include:

- `binding`
- `frozen_semantics`
- `frozen_scope`
- `phase_route`
- `target_phase`
- `team_spec`
- `task_spec`
- `task_team_mapping`
- `completion_contract`
- `report_contract`
- `allowed_actions`
- `allowed_tools`
- `approval_requirements`

One bridge window binds exactly one team and one task. The task may have multiple teammate assignments, but the packet must not describe multiple independent tasks.

## 6. Lifecycle

The authoritative lifecycle is the bridge-window state machine in:

`.claude/control/policy/lifecycle_transition_table.json`

Normal path:

`bridge_call_intended -> bridge_call_prechecked -> bridge_call_started -> bridge_window_opened -> bridge_packet_accepted -> team_create_completed -> task_create_completed -> task_created_recorded -> message_dispatch_completed -> team_waiting/artifacts_ready -> task_completion_completed -> team_delete_completed -> bridge_window_returned`

Failure/recovery facts are first-class:

- `bridge_call_denied`
- `bridge_call_failed`
- `bridge_packet_rejected`
- `team_create_failed`
- `task_create_failed`
- `message_dispatch_failed`
- `team_wait_timeout`
- `task_completion_rejected`
- `task_failed`
- `team_delete_failed`
- `bridge_window_partial_returned`
- `bridge_window_orphaned`

Absence of a matching end event is meaningful and may become an orphan condition.

## 7. Snapshot And Notify

`RuntimeSnapshot` contains:

- frozen semantic/scope state
- route state
- lifecycle status index
- bridge/team/task/tool bindings
- allowed actions
- allowed routes
- integrity alerts
- last bridge result
- phase exit readiness

`NotifyResult` writes items to the main-leader inbox. Blocking/error/warn/info messages are derived from check results, update results, integrity state, bridge results, TeamIdle, timeout, cleanup, and orphan events.

## 8. Long-Running Work

Long-running work is represented with `TeamIdle` events and payloads such as:

- `wait_reason`
- `owned_process_refs`
- `last_heartbeat_at`
- `timeout_policy`
- `artifact_probe`
- `partial_reports`
- `partial_artifact_refs`

`TeamIdle` means waiting, not completion. Completion requires evidence satisfying the completion contract.

## 9. Active Runtime Entry Point

The Claude Code entry point is self-contained in this parent-sibling `.claude` directory.
The `bridge` MCP server is declared in `.claude/settings.json`:

`bridge -> python ../.claude/control/mcp/bridge_server.py`

Claude Code exposes its tools as `mcp__bridge__...` tools.

The active CLI accepts workflow events only:

`python ../.claude/control/runtime/main.py --control-root ../.claude/control --event-json '{...}' --persist`

Legacy task-action requests are not accepted by this entry point.

## 10. Completion Standard

The system is behaving correctly only when:

- main-leader remains the sole front-facing controller
- downstream work occurs through bridge windows
- every bridge window has exactly one team and one task
- every meaningful lifecycle fact is durable
- denied and failed actions preserve their prior intent/start facts
- runtime snapshot can be reconstructed from ledgers
- bridge results are artifact/report backed
- interrupted or orphaned work can be detected from state
