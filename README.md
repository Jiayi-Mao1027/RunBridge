# Parent-Sibling Claude Workflow

This repository contains a Claude Code workflow control plane for running governed multi-agent work from a sibling target repository.

The short version: keep the workflow system in `workspace-parent/.claude`, start Claude from `workspace-parent/your-repo`, and let the parent control plane manage routing, lifecycle, bridge invocation, agent roles, policy checks, and runtime ledgers. The target repo stays clean.

```text
workspace-parent/
  .claude/        <- workflow system, agents, hooks, MCP server, policies, runtime state
  your-repo/      <- target repo under work; no workflow files required
```

## Mental Model

This is not just a set of Claude agent prompts. It is a stateful workflow runtime around Claude Code.

The main session is owned by `leader-orchestrator`. The leader does not directly spin up arbitrary agents or mutate the repo whenever it feels like it. Instead, it reads runtime truth, freezes the user's request into a stable task meaning, asks the runtime which phase/route is legal, builds one `BridgePacket`, and opens one bridge window through MCP.

Inside that bridge window, `bridge-leader` owns one team and one task. It may create teammate agents such as preflight, curator, implementor, rungater, or anomaly analysts, but those teammates are bounded by the packet. They report evidence back to the bridge leader, and the bridge leader returns one structured bridge result to the main leader.

The important invariant is:

```text
one bridge invocation -> one bridge window -> one team -> one task -> one result
```

The team may contain multiple teammates, but the bridge window is not allowed to become a free-form batch of unrelated tasks.

## Why This Exists

Claude Code can already call tools and spawn agents. This workflow adds a control plane for cases where that is not enough:

- You need to know whether a failure is a small teammate mistake or a system-level control-plane problem.
- You need lifecycle facts such as "bridge call intended", "pretool allowed", "window opened", "team created", "task completed", and "result returned".
- You need phase routing, so implementation work does not happen before preflight or after an unresolved hard stop.
- You need replayable ledgers instead of only natural-language summaries.
- You want to reuse one workflow system across many repos without copying `.claude` files into each repo.

## Normal Flow

```text
user request
  -> leader-orchestrator reads runtime snapshot
  -> leader-orchestrator freezes task semantics
  -> runtime validates allowed phase route
  -> bridge MCP builds one BridgePacket
  -> main leader calls bridge SDK through MCP
  -> bridge-leader accepts the packet
  -> bridge-leader creates one team and one task
  -> teammates do bounded work and report evidence
  -> bridge-leader validates completion and deletes the team
  -> bridge result returns to main leader
  -> main leader resumes from runtime truth
```

This is why the runtime state matters. The leader is supposed to resume from the ledger and snapshot, not from memory or a guessed prompt narrative.

## Roles

The system ships agent prompts under `.claude/agents/`. Common roles include:

- `leader-orchestrator`: front-facing main leader. Reads runtime truth, freezes semantics, chooses route, opens bridge windows, reports system-level issues.
- `bridge-leader`: owner of exactly one bridge invocation window. Creates the team/task, dispatches teammates, collects reports, returns bridge result.
- `preflight-initial`: inspects the target repo and surfaces implementation blockers before mutation.
- `curator`: keeps the active downstream surface minimum viable by archiving stale or ambiguous logs, datasets, checkpoints, outputs, scratch code/scripts, and misleading inactive documents before preflight and implementation. It has restricted Bash authority for filesystem curation only.
- `refresher`: performs bounded documentation refresh when the packet allows it.
- `implementor`: makes approved code/config changes inside the target repo boundary while preserving a minimum viable active project surface.
- `rungater`: checks readiness after implementation and recommends proceed, repair, reroute, or stop.
- `anomaly-analyst-*`: investigates failed, partial, blocked, or orphaned workflow states.

Roles are selected by phase, policy, and risk-based packet construction. Low-risk clear tasks may use a reduced policy-valid team, while explicit high-risk, write, execute, anomaly, or multi-view tasks keep the fuller phase team. The `team_planning` packet field records the selector, risk profile, original teammate names, selected teammate names, and policy ref. A teammate is not supposed to infer broad authority from being spawned.

## Phases

Phase routing is policy-owned. The current phase determines which next routes are legal.

Typical phases:

- `leader_freeze`: main leader reads the request and freezes execution-relevant semantics.
- `l2_advisory`: optional advisory/planning route when the task needs sharpening.
- `l3_bridge`: bridge/preflight/documentation-oriented route.
- `l4_implement`: implementation-facing work.
- `l4_execute`: validation, execution, and post-run checks.
- `l4_anomaly`: recovery path for failed, partial, blocked, or orphaned windows.

From `l3_bridge`, the graph intentionally allows routing to every phase: another L3 pass, `leader_freeze`, `l2_advisory`, `l4_implement`, `l4_execute`, or `l4_anomaly`. This makes L3 the bridge hub: it can perform a minimal repo sanity check after L2, resume after user confirmation, send ambiguous strategy back to L2, or send execution/result questions to L4 anomaly before implementation or execution proceeds.

L3 packets have a documentation responsibility. When the work touches docs, Markdown, `CLAUDE.md`, README, setup/usage guidance, workflow rules, or agent behavior, L3 must explicitly decide whether repo-facing documentation needs a bounded update. `CLAUDE.md` is a first-class L3 target for workflow and agent-behavior changes.

L3 packets also carry a minimum-active-surface responsibility. Curator should first understand the current step, what prior work has already completed, and what the next phase actually needs; then it should archive stale, duplicate, ambiguous, or non-current datasets, checkpoints, generated outputs, stale code copies, scratch scripts, and misleading inactive documents out of active reach. Logs are cleaned more conservatively: retain logs that may be reused for comparison, audit, avoiding expensive regeneration, or downstream interpretation; archive only logs that are clearly unused, duplicate, superseded, unrelated, or misleading. Archive is the default for material with possible audit value. Physical deletion is reserved for clearly disposable material or explicit approval.

Within L3, only `curator` may receive `Bash`, and that authority is limited to non-executing filesystem curation inside packet writable scopes: creating archive directories, moving files/directories, or deleting clearly disposable trash/empty duplicates. Curator should prefer native PowerShell filesystem cmdlets such as `New-Item`, `Move-Item`, and `Remove-Item -LiteralPath`, verify absolute paths before recursive move/delete, and report every move/delete with source, destination or deletion basis, and reason. `preflight-initial` and `refresher` remain no-shell roles; they inspect or edit documentation with their bounded read/write tools.

L3 packets carry a semantic-resolution responsibility as well. When model/method names, checkpoints, datasets, prompts, configs, metrics, or comparisons are involved, L3 must resolve the concrete identities or explicitly mark them blocked/escalated. If the user did not request changing dataset, prompt, split, metric, or config, the packet should preserve the current active basis and name where that basis came from, so L4 does not guess.

L3 packets also carry current user intent context. The main leader should preserve the nearest active direction, relevant L2 report refs or summary, proposed future directions, and open questions in the task spec. L3 must confirm, refine, supersede, block, or escalate that intent from repo/docs/artifact evidence and report the disposition so the next phase does not guess. For example, an OPD early-stop improvement proposed after L2 remains active context until L3 evidence or a later user instruction changes it.

L4 implement inherits that hygiene requirement. Implementors should modify existing files when practical, use temporary scripts for one-off work, create long-lived files only for durable need, and avoid handing rungater/executor an active surface cluttered with exploratory logs, scratch scripts, stale checkpoints, duplicate code copies, or stale data.

L4 execute is intentionally different from short implementation or review windows. It may own long-running training or evaluation jobs. For L4 execute, `TeamIdle` means the team is waiting or polling; it is not completion and is not a reason to delete the team. If executor launches an owned long-running process, the bridge window should remain open until the process reaches a terminal state and postrun has audited terminal logs/artifacts.

L4 execute also treats smoke parameters as evidence, not as the final run shape. Executor should run bounded smoke checks to choose formal per-device batch size, microbatch, gradient accumulation, precision, sequence length, and effective batch size, and postrun should audit that formal settings follow that evidence.

Every generated formal log folder must contain an internal manifest, analogous to checkpoint manifests. The manifest is the durable identity record and should include run/window/task IDs, command, cwd, environment, semantic basis, smoke evidence refs, formal parameters/effective batch size, process refs, log files, expected outputs/checkpoints, timestamps, terminal status, and reuse/dependency notes. File names alone are not sufficient.

L4 execute has strict environment and GPU rules. Formal execution uses conda env `mjy`; use `conda run -n mjy ...` or record an equivalent `conda activate mjy` context, and do not use venv/virtualenv for formal execute. Unless the user explicitly requests smoke/dry-run/conservative execution, formal GPU runs must exceed 70GB observed memory after warmup on typical 80GB GPUs, or exceed 90% of selected GPU total memory on other GPU sizes; lower usage is a deviation or blocker unless backed by explicit approval or hard resource evidence.

If one execute bridge/session contains multiple formal stages, such as train followed by value/evaluate/score, the GPU memory target applies separately to each formal stage. The executor must not reuse train-stage batch or memory evidence as proof that later stages are configured correctly; each stage needs its own batchbasis, observed memory evidence, and pass/deviation/block classification.

Executor Bash hooks add soft reminders rather than hard process control. Formal-looking executor Bash commands may be annotated in `tool_events.jsonl` with missing GPU probe, batch/effective-batch basis, or log-manifest reminders. Smoke/dry-run/debug commands are not killed for low memory and receive only smoke-appropriate reminders.

The phase is not just a final label. It is a runtime trace of important action intent, action start, action end, denial, failure, partial completion, and orphaning. This allows later audit to distinguish "never attempted" from "attempted and failed" from "started but never returned".

Manual bridge interrupts are terminal runtime facts, not permanent blockers. If the user interrupts a bridge invocation, the runtime records `bridge_call_interrupted` and closes that window as `bridge_window_interrupted`, so later work can resume from the snapshot instead of being blocked by a stale open bridge window.

## BridgePacket

`BridgePacket` is the packet the main leader sends into one bridge window. It is rebuilt for each bridge invocation and should not be treated as a global run plan.

It carries:

- run/session/window binding IDs
- frozen semantics and frozen scope
- phase route and target phase
- team specification
- task specification
- task-to-team mapping
- completion contract
- report contract
- allowed actions/tools
- approval requirements owned by runtime policy

The bridge leader must stay inside the packet. The packet defines what can be read, written, delegated, reported, retried, or stopped.

Task specs preserve compound user intent instead of reducing it to one short description. The packet includes the original instruction, an `instruction_coverage_checklist`, `current_user_intent_context`, and preserved context fields. Teammate assignments must report whether each checklist item was completed, deferred with a concrete reason, blocked, or escalated. This prevents a multi-part user request from being half-executed and then treated as complete.

Task specs also include `semantic_resolution_contract`. Runtime packet validation requires the report contract to include semantic identity resolution evidence, so downstream packets cannot silently drop checkpoint/dataset/prompt/config identity work. L4 execute packets additionally require log manifest artifact evidence.

## Runtime And Ledgers

Runtime state is stored under the parent `.claude` tree:

```text
.claude/runtime_state/projects/<repo-key>/runs/
```

The repo key is derived from the target repo path, so multiple sibling repos can share the same parent control plane while keeping separate run ledgers.

Per run, the runtime writes:

- `run_ledger.json`: authoritative mutable run state
- `runtime_snapshot.json`: compact routing and recovery truth for leaders and tools
- `event_log.jsonl`: canonical authoritative workflow events; each persisted record also carries `runtime_event` (`runtime_event_envelope.v1`)
- `check_ledger.jsonl`: check decisions and reasons
- `update_ledger.jsonl`: persisted update results
- `transitions.jsonl`: lifecycle transition facts
- `main_leader_inbox.jsonl`: notifications for the main leader

Runtime data is split into three layers:

- Authoritative state: canonical event log, run ledger, task/window lifecycle transitions, indexes, and compact snapshot refs used for recovery/routing.
- Artifacts: BridgePacket, BridgeResult, teammate reports, completion reports, manifests, tool outputs, evidence files, and structured `artifact_ref.v1` records with producer/run/window/task/hash metadata.
- Projections: Companion timeline, cards, brief text, trajectory views, and other UI views. These can be deleted and rebuilt from authoritative state plus artifacts; they are never a main-leader recovery source.

Stream and observer inputs should normalize into `runtime_event_envelope.v1` before becoming runtime facts or projections. The envelope records source (`outer_sdk`, `inner_sdk`, `cli`, `hook`, `runtime`, `companion`) and authority (`authoritative`, `source`, `observed`, `derived`, `projection`) so UI rendering does not blur runtime truth with observations.

Bridge-window bindings keep compact packet attribution (`packet_ref`, `packet_hash`, `target_phase`) in the run ledger. Completion validation reloads the original packet from the authoritative event log when available, so L4 lifecycle and report/artifact checks are evaluated against the packet boundary rather than a later UI projection or natural-language report.

`runtime_snapshot.json` is intentionally compact. It is a routing and recovery view for the main leader, not a place for full reports, tool logs, full evidence, or complete historical bindings. Large details remain in ledgers and observer streams, and the snapshot carries `snapshot_refs`, counts, and short previews so the leader can open details only when needed.

The snapshot also carries compact runtime diagnostics. A bridge window that is open too long, stuck after message dispatch, and has no process refs, reports, artifacts, or completion checks is classified as `workflow instability / bridge orchestration hang`. The correct leader behavior is to stop ordinary waiting, inspect the diagnostic refs, state the orchestration hang to the user, then mark orphaned, reroute, or retry from evidence.

Diagnostics also include execute watchdog warnings. A bridge window in `team_waiting` with owned process refs and a stale heartbeat is classified as `execute_stale_heartbeat_with_owned_process_refs`; this means the leader should inspect process refs, process events, active operations, logs, artifact refs, and output dirs instead of waiting for the hard timeout blindly.

### Runtime Snapshot Size Discipline

The snapshot is read frequently and can enter model context. Treat it as a control-plane index, not as an evidence warehouse. It should answer only the next routing questions: what run/window is active, what phase is legal, what lifecycle facts matter, what approval or hard stop exists, what teammate/session bindings are current enough to route, and where the full details live.

Keep large or historical material out of `runtime_snapshot.json`:

- Do not store full bridge reports, complete teammate transcripts, complete tool outputs, full diffs, large stdout/stderr, full prompts, full artifact manifests, or all historical session bindings.
- Store counts, latest/open items, stable IDs, short safe previews, and `snapshot_refs` pointing to authoritative files.
- Keep `last_bridge_result` as a compact result index: report/artifact counts, status/checklist summaries, bounded previews, and refs to the full bridge result or observer streams.
- Keep `bindings` as current attribution, not history: active/open bridge windows, recent session bindings, recent tool-use IDs, and omitted-count metadata.
- Preserve `semantic` and `scope` carefully because packet validation depends on frozen equality. If those fields become too large, change validation to hash/ref semantics first; do not ad hoc truncate them.

Main leader behavior should follow the same rule: read the snapshot first, then follow `snapshot_refs` only for the specific evidence needed to make the next decision.

The runtime also writes read-only Bridge Companion observer streams. These are not authoritative workflow state; they are structured side-channel facts for UI/debug display:

- `bridge_packets.jsonl`: packet summary, user instruction, scope, team, completion/report contract
- `agent_messages.jsonl`: bridge-leader to teammate assignment messages and checklist coverage refs
- `tool_events.jsonl`: real tool starts/completions, safe input previews, file refs, output summary, duration when available
- `session_bindings.jsonl`: session-to-run/team/task/teammate binding facts for UI attribution
- `session_events.jsonl`: safe session-level previews for prompts, tool starts/completions, stops, and session end
- `teammate_reports.jsonl`: structured progress, completed/open/blocked items, evidence refs, file refs
- `process_events.jsonl`: long-running process refs, PID/state/heartbeat/log/artifact probe
- `artifacts.jsonl`: artifact references recorded from runtime events
- `completion_checks.jsonl`: completion/checklist item disposition
- `companion_events.jsonl`: merged observer stream with source backrefs

The runtime also writes native RunBridge durability and replay artifacts:

- `checkpoints.jsonl`, `checkpoints/*.json`, and `latest_checkpoint.json`: compact state checkpoints written after persisted runtime events.
- `trajectory.jsonl` and `trajectory_index.json`: UI-safe research/execution timeline steps derived from workflow events and real tool events. These include intent/action/observation/state-delta summaries, not hidden chain-of-thought or full unbounded outputs.

Tool observer records are emitted for all Claude Code sessions, not only bridge child sessions. Records include `session_kind`, `run_binding_state`, `session_id`, run/window/team/task IDs when available, `teammate_id`, `agent_type`, `tool_name`, `tool_use_id`, and `status`. If a hook cannot bind a tool event to a run, it writes the safe preview to `.claude/runtime_state/session_observer/` so Companion can still show direct or unbound session activity.

UI must not synthesize low-level actions from reports or artifact refs. It should show `Read` / `Edit` / `Write` / `MultiEdit` / `Bash` / `Grep` / `Glob` / `LS` only when those real hook records exist in `tool_events.jsonl`. The hooks bind teammate child sessions from `SubagentStart` payloads such as `agent_name` / `subagent_name` when present, write that binding to `session_bindings.jsonl`, and rebind later child-session tool events by `session_id` when a tool payload lacks direct run fields. This is what lets real subagent tool calls land in the run-scoped observer stream with run/window/team/task/teammate attribution.

For executor Bash events, UI may surface `soft_reminders` as nonblocking evidence prompts. They are not failures and do not imply the process was stopped.

For UI-style "what is happening now" display, hooks maintain `active_operations.json` beside the run observer files, and a global `.claude/runtime_state/session_observer/active_operations.json` for unbound sessions. This snapshot is derived from tool started/completed pairs and contains the active tool and last completed tool per session/teammate.

Observer records include run-local `sequence` / `monotonic_index` fields for stable UI ordering. The merged stream includes `source_kind`, `source_file`, `source_sequence`, and `source_offset` so the UI can trace a rendered item back to its typed JSONL source. Observer writes are best-effort; an observer failure must not block normal runtime check/update/notify behavior.

Generated runtime folders such as `.claude/runtime_state/` and `.claude/worktrees/` can be cleared when no workflow is running, but doing so deletes historical debug/replay evidence.

## Lifecycle And Recovery

The lifecycle includes normal bridge execution states as well as explicit clarification and recovery states:

- `blocked_for_user_clarification`
- `paused_for_user_answer`
- `user_answer_received`
- `resume_same_l3_task`
- `continuation_of_previous_l3`

If L3 needs user confirmation, the main leader should ask the user, record the answer, and continue the same L3 task through the legal L3 hub route. The user should not need to manually say "reroute"; bridge-denial notifications include a recommended legal next route when one exists.

For L4 execute, premature partial returns are treated as a protocol failure when owned process refs still show a running process. In that case the runtime records `L4ExecutePrematurePartialReturn` instead of accepting the result as a normal partial bridge completion.

## Failure Model

The workflow intentionally separates lower-level agent errors from system-level problems.

Lower-level teammate issues include small tool mistakes, missing optional context, transient read errors, or a teammate needing to retry within its packet boundary. The bridge leader should handle these without interrupting the user when possible.

System-level problems include:

- lifecycle transition rejected by runtime
- route not allowed by current phase
- bridge window opened but never returned
- packet binding mismatch
- hard stop or pending approval
- MCP/SDK/control-plane failure
- target repo boundary violation
- bridge executor returning empty, malformed, or incomplete structured output
- L4 execute returning partial while an owned long-running process is still running

Those are not supposed to be silently bypassed. They should be recorded and surfaced.

## Target Repo Boundary

The target repo should not contain workflow system files.

Do not place these in the target repo:

- `.claude/`
- `.mcp.json`
- copied agent prompts
- runtime ledgers
- bridge prompt audit files
- temporary agent worktrees

All workflow files live in the parent `.claude`. The target repo only contains the actual project being worked on.

## Startup

The migration target is a long-lived outer SDK host plus a separate read-only Companion gateway. The host is the process that may accept user input and write runtime facts; Companion can forward input to it but does not become the scheduler or truth source.

Start the outer host from inside the target repo:

```powershell
cd C:\path\to\workspace-parent\your-repo
python ..\.claude\control\runtime\outer_sdk_host.py --control-root ..\.claude\control --repo-root . --main-session-id outer-main
```

Current status of this path:

- The host is long-lived and process-owned.
- `POST /v1/input` records `user_prompt_submitted` or `user_answer_received` through `workflow_runtime`.
- It writes `outer_host_events.jsonl` and SDK-shaped `sdk_stream_events.jsonl` for Companion.
- The default adapter is the outer Claude Agent SDK wrapper (`--adapter auto`, equivalent to SDK-first). It owns one persistent SDK client per host process and normalizes SDK messages into `sdk_stream_events.jsonl`.
- If `claude-agent-sdk` is not installed, the host records the input and returns `OuterLeaderSdkDependencyMissing` instead of pretending leader reasoning ran. Use `--adapter unavailable` only for fallback/debug smoke.

Start Companion separately:

```powershell
cd C:\path\to\workspace-parent\bridge-companion
$env:BRIDGE_OUTER_HOST_URL="http://127.0.0.1:8791"
node gateway\server.mjs
```

### Claude CLI Compatibility

Claude Code must load both parent-level configuration files:

- `../.claude/settings.json` for agent, environment, and hooks
- `../.claude/mcp.json` for the bridge MCP server

Start from inside the target repo:

```powershell
cd C:\path\to\workspace-parent\your-repo
claude --settings ../.claude/settings.json --mcp-config ../.claude/mcp.json --strict-mcp-config
```

This remains a compatibility/debug path. It is not the target facing-leader topology; the target is `outer_sdk_host.py` with the SDK adapter and Companion forwarding input to that host.

Use a wrapper if desired:

```powershell
function cc {
  claude --settings ../.claude/settings.json --mcp-config ../.claude/mcp.json --strict-mcp-config @args
}
```

`--strict-mcp-config` is recommended so Claude does not silently mix in repo-local MCP config.

This remains the compatible fallback/debug startup while the real outer SDK adapter is being wired. It is no longer the intended long-term architecture.

## Key Configuration

Main configuration lives in:

```text
.claude/settings.json
.claude/mcp.json
```

It defines:

- environment defaults such as `PYTHONNOUSERSITE=1` and `PYTHONDONTWRITEBYTECODE=1`
- the default front-facing session agent, `leader-orchestrator`
- hooks pointing back to `.claude/hooks/*.py`
- the `bridge` MCP server pointing to `.claude/control/mcp/bridge_server.py`

The bridge MCP exposes:

- `mcp__bridge__read_runtime_snapshot`
- `mcp__bridge__build_bridge_packet`
- `mcp__bridge__call_bridge_sdk`
- `mcp__bridge__dispatch_workflow_event`
- `mcp__bridge__reconcile_workflow_from_ledger`

## Important Files

```text
.claude/CLAUDE.md                         Main operating contract
.claude/settings.json                     Agent, environment, and hook configuration
.claude/mcp.json                          Active bridge MCP server configuration
.claude/agents/leader-orchestrator.md     Main leader instructions
.claude/agents/bridge-leader.md           Bridge-window owner instructions
.claude/agents/*.md                       Teammate role instructions
.claude/control/mcp/bridge_server.py      MCP bridge server
.claude/control/runtime/workflow_runtime.py
.claude/control/runtime/state_graph.py
.claude/control/runtime/checkpoint_store.py
.claude/control/runtime/retry_policy.py
.claude/control/runtime/trajectory.py
.claude/control/runtime/output_guardrails.py
.claude/control/runtime/repo_runtime.py
.claude/control/runtime/main_leader.py
.claude/control/runtime/bridge_sdk.py
.claude/control/runtime/bridge_leader.py
.claude/control/runtime/claude_cli_executor.py
.claude/control/runtime/outer_sdk_host.py
.claude/control/runtime/outer_sdk/
.claude/control/runtime/companion_observer.py
.claude/hooks/*.py                        Claude hook adapters
.claude/control/policy/*.json             Lifecycle, phase, approval policy
.claude/control/policy/state_graph.json   Native RunBridge state graph policy
.claude/control/policy/phase_contracts.json
                                           System-owned phase/team/tool/report/manifest contracts
.claude/control/schemas/*.json            Runtime data contracts
pseudocode/main_workflow.md               High-level intended workflow
bridge-companion/                         Optional read-only UI/gateway for observer streams
```

## Verification

From this source package:

```powershell
python .claude/control/mcp/verify_bridge_mcp.py
python .claude/control/runtime/smoke_test.py
```

For smoke-only bridge execution:

```powershell
$env:BRIDGE_EXECUTOR='simulate'
```

Bridge execution now goes through the `BridgeExecutor` interface:

- `BRIDGE_EXECUTOR=cli` (default): existing Claude CLI `stream-json` subprocess path, retained as fallback/debug/canary.
- `BRIDGE_EXECUTOR=simulate`: deterministic smoke executor using the same interface.
- `BRIDGE_EXECUTOR=sdk`: SDK-in-SDK migration skeleton. It reports `SdkExecutorNotImplemented` until the inner SDK session is wired.

Without `BRIDGE_EXECUTOR=simulate` or `BRIDGE_EXECUTOR=sdk`, the bridge executor uses the existing nested non-interactive Claude Code call through `claude -p`.

## Notes

- No user-level install is required.
- No `~/.claude` changes are required.
- No repo-local workflow code, `.claude/` directory, or `.mcp.json` file is required.
- The only required shared artifact is the sibling parent `.claude` directory.
- The startup command or wrapper must pass both `--settings ../.claude/settings.json` and `--mcp-config ../.claude/mcp.json`.
