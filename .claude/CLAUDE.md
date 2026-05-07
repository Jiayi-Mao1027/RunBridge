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

The task spec must preserve the full user-facing intent, not only a shortened description. Complex instructions should be carried as `original_user_instruction`, `instruction_coverage_checklist`, and preserved context fields. Downstream assignments must require every checklist item to be completed, explicitly deferred with a concrete reason, or escalated; reports must include the same coverage disposition. This prevents main-leader from executing only the first or easiest half of a compound request.

The task spec must also carry a `semantic_resolution_contract`. L3 must actively resolve model/method, checkpoint, dataset/split, prompt/template, config, metric/objective, and inherited-default identities before L4 inherits the task. If the user did not ask to change dataset, prompt, split, metric, or config, the downstream packet should preserve the current active basis and name the evidence for that inheritance. L4 implement and execute must not silently guess or swap unresolved semantic identities.

L3 bridge packets must make repository-facing documentation freshness explicit. When a task touches docs, Markdown, `CLAUDE.md`, README, setup/usage instructions, agent behavior, or workflow rules, the L3 task should require the team to inspect whether docs need updating and to make the smallest correct update inside writable scope. `CLAUDE.md` is a first-class L3 documentation target when workflow or agent behavior changes.

L3 also owns minimum-viable active-surface curation. Before preflight or implementation proceeds, curator should establish what the current step is, what prior work is already completed, and which files/artifacts are genuinely required by the next phase. Stale, duplicate, ambiguous, or non-current datasets, checkpoints, generated outputs, stale code copies, scratch scripts, and misleading inactive documents should be archived out of active reach by default. Logs are cleanup targets but are more nuanced: retain logs that may be reused for comparison, audit, avoiding expensive regeneration, or downstream interpretation; archive only logs that are clearly unused, duplicate, superseded, unrelated, or misleading. Physical deletion is exceptional and should be limited to clearly regenerable trash, empty duplicates, or explicitly approved removals. L3 may archive or organize project files, but code/config behavior changes belong to L4 implement.

L4 implement must preserve the same minimum-viable project surface while changing code. Implementors should prefer modifying existing files, use temporary scripts for one-off work, create new long-lived files only when there is a durable need, and clear or archive implementation byproducts before rungater/executor inherit the repo.

L4 execute treats smoke parameters and formal parameters as different decisions. Executor should run bounded smoke checks to choose formal per-device batch size, microbatch, gradient accumulation, precision, sequence length, and effective batch size. Postrun audits that the formal command used the resolved semantic basis and smoke-derived parameter evidence.

L4 execute must write a manifest inside every generated formal log folder, analogous to checkpoint manifests. The manifest is the durable identity record for the log folder and should include run/window/task IDs, command, cwd, environment, semantic basis, smoke evidence refs, formal parameters/effective batch size, process refs, log files, expected outputs/checkpoints, timestamps, terminal status, and reuse/dependency notes. Do not rely on folder or file names alone.

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
- `blocked_for_user_clarification`
- `paused_for_user_answer`
- `user_answer_received`
- `resume_same_l3_task`
- `continuation_of_previous_l3`

Absence of a matching end event is meaningful and may become an orphan condition.

Open-window orchestration anomalies are first-class routing facts. If a bridge window stays open past the anomaly threshold while stuck in an early lifecycle state, especially `message_dispatch_completed`, and there are no process refs, teammate reports, artifact refs, or completion checks, main-leader should stop ordinary waiting and classify it as `workflow instability / bridge orchestration hang`. The next response is diagnostic: inspect `runtime_snapshot.runtime_diagnostics`, event log/transitions, process/report/artifact refs, known output dirs, and known logs before retrying, marking orphaned, or rerouting to anomaly work.

Execute watchdog alerts are first-class warning facts. If a bridge window is in `team_waiting` with owned process refs that still look running but `last_heartbeat_at` is stale, `runtime_diagnostics.execute_watchdog_alerts` reports `execute_stale_heartbeat_with_owned_process_refs`. This is not proof of failure or completion; main-leader should inspect process refs, process events, active operations, logs, artifact refs, and known output dirs before deciding whether to poll, keep waiting, reroute to anomaly, or classify process loss.

## 7. Snapshot And Notify

`RuntimeSnapshot` contains:

- compact snapshot policy and refs to full ledgers/observer streams
- frozen semantic/scope state
- route state
- lifecycle status index
- compact bridge/team/task/tool binding summaries
- allowed actions
- allowed routes
- integrity alerts
- compact runtime diagnostics, including bridge orchestration hang candidates
- compact last bridge result summary
- phase exit readiness

`RuntimeSnapshot` is intentionally a compact control view, not a transcript, report store, or evidence bundle. It should preserve enough information for main-leader routing and recovery without pulling long reports, full evidence, complete tool streams, or historical binding maps into context. Full details remain in `run_ledger.json`, `event_log.jsonl`, `transitions.jsonl`, `main_leader_inbox.jsonl`, and observer JSONL files referenced by `snapshot_refs`.

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

For long-running execution work, especially L4 execution/training, the execution group must estimate expected wall-clock runtime before launch and include the estimate basis in the execution record. A bridge soft timeout or partial result means the bridge window stopped waiting and returned intermediate state; it does not by itself prove the owned process was killed, failed, or completed.

For L4 execute, the intended contract is stronger: if the executor launches an owned long-running process, the bridge window must remain open until the process reaches a terminal state and postrun has audited terminal logs/artifacts. `TeamIdle` is waiting/progress evidence, not permission to delete the team or return a partial bridge result while the owned process is still running. The execute timeout policy is sized for long training runs and should not use the short 900 second bridge-window default.

L4 execute environment and GPU policy are strict. Formal execution commands must run under conda env `mjy`, preferably via `conda run -n mjy ...` or an explicitly recorded equivalent `conda activate mjy` shell context. Do not use `venv`, `.venv`, `virtualenv`, or ad hoc Python environments for formal execute. For formal GPU training or throughput-sensitive runs, unless the user explicitly requested smoke/dry-run/conservative execution, executor must configure the run to exceed 90% of selected GPU total memory after warmup; on typical 80GB GPUs this usually means observed usage above 70GB. Lower formal-run utilization requires explicit conservative approval or hard blocking evidence and must be surfaced as a deviation, not treated as success.

Executor Bash hooks are soft guardrails, not killers. For executor-owned Bash commands that look like formal GPU training/evaluation, PreToolUse/PostToolUse may write `soft_reminders` into `tool_events.jsonl` and `session_events.jsonl` when GPU memory probes, batch/effective-batch basis, or log manifest evidence is missing. Hooks must not kill a process solely because current memory use is low, and smoke/dry-run/debug commands should receive only smoke-appropriate reminders.

## 9. Companion Observability

Bridge Companion is a read-only observer. It must not enter agent context, control routing, create tasks, message teammates, or write authoritative workflow state.

The runtime writes side-channel JSONL observability files under each run directory for Companion:

- `bridge_packets.jsonl`
- `agent_messages.jsonl`
- `tool_events.jsonl`
- `session_bindings.jsonl`
- `session_events.jsonl`
- `teammate_reports.jsonl`
- `artifacts.jsonl`
- `completion_checks.jsonl`
- `process_events.jsonl`
- `companion_events.jsonl`

Observer records include a run-local `sequence` / `monotonic_index` for stable UI ordering. `companion_events.jsonl` is a merged stream and includes `source_kind`, `source_file`, `source_sequence`, and `source_offset` so UI drawers can trace every merged item back to its typed JSONL source.

`tool_events.jsonl` should expose UI-safe fields only: `session_kind`, `run_binding_state`, `session_id`, run/window/team/task IDs when available, `teammate_id`, `agent_type`, `tool_name`, `tool_use_id`, `status`, `started_at`, `completed_at`, `duration_ms`, `normalized_input`, `safe_input_preview`, `file_refs`, and bounded summaries such as `read_options`, `edit_summary`, `search_summary`, `command_preview`, `stdout_tail`, and `stderr_tail`. It must not include secrets, full prompts, or complete large file contents. Tool events are observed for all Claude Code sessions, not only bridge child sessions. If no run binding is available, hooks write safe records under `.claude/runtime_state/session_observer/` instead of dropping them.

`session_bindings.jsonl` maps `session_id` to run/window/team/task/teammate identity when known. `session_events.jsonl` records safe session-level previews such as user prompt, tool call started/completed, stop, and session end. Hooks also maintain `active_operations.json` for run-bound sessions and `.claude/runtime_state/session_observer/active_operations.json` for unbound sessions so UI can render the current active tool without replaying the whole stream.

`agent_messages.jsonl` should include `message_id`, `direction`, `coverage_refs`, and whether a response is required. `tool_events.jsonl` must contain real Claude Code tool calls such as `Read`, `Edit`, `Write`, `MultiEdit`, `Bash`, `Grep`, `Glob`, and `LS` when those hooks fire; UI must not invent low-level actions from reports or artifacts. If a child tool event lacks direct run fields, hooks should rebind it from the latest `session_bindings.jsonl` record for the same `session_id` before writing the run-scoped observer stream. `tool_events.jsonl` may include `soft_reminders` for executor Bash commands; these are nonblocking prompts to collect missing evidence, not runtime denials. `teammate_reports.jsonl` should include structured progress fields such as `progress_state`, `completed_items`, `open_items`, `blocked_items`, `evidence_refs`, and `file_refs`. `completion_checks.jsonl` should include checklist-level `items` with per-item status and evidence/reason fields. `process_events.jsonl` is the read-only place for long-running process state such as PID, command preview, heartbeat, terminal state, log tail ref, and artifact probe.

These files are derived from runtime events and Claude Code hooks. They are for UI/debug inspection only; authoritative workflow truth remains `run_ledger.json`, `event_log.jsonl`, task ledgers, transitions, and `runtime_snapshot.json`.

## 10. Active Runtime Entry Point

The Claude Code entry point is self-contained in this parent-sibling `.claude` directory.
The `bridge` MCP server is declared in `.claude/settings.json`:

`bridge -> python ../.claude/control/mcp/bridge_server.py`

Claude Code exposes its tools as `mcp__bridge__...` tools.

The active CLI accepts workflow events only:

`python ../.claude/control/runtime/main.py --control-root ../.claude/control --event-json '{...}' --persist`

Legacy task-action requests are not accepted by this entry point.

## 11. Completion Standard

The system is behaving correctly only when:

- main-leader remains the sole front-facing controller
- downstream work occurs through bridge windows
- every bridge window has exactly one team and one task
- every meaningful lifecycle fact is durable
- denied and failed actions preserve their prior intent/start facts
- runtime snapshot can be reconstructed from ledgers
- bridge results are artifact/report backed
- interrupted or orphaned work can be detected from state
