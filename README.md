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
- `curator`: keeps the active downstream surface minimum viable by archiving stale or ambiguous logs, datasets, checkpoints, outputs, scratch code/scripts, and misleading inactive documents before preflight and implementation.
- `refresher`: performs bounded documentation refresh when the packet allows it.
- `implementor`: makes approved code/config changes inside the target repo boundary while preserving a minimum viable active project surface.
- `rungater`: checks readiness after implementation and recommends proceed, repair, reroute, or stop.
- `anomaly-analyst-*`: investigates failed, partial, blocked, or orphaned workflow states.

Roles are selected by phase and packet construction. A teammate is not supposed to infer broad authority from being spawned.

## Phases

Phase routing is policy-owned. The current phase determines which next routes are legal.

Typical phases:

- `leader_freeze`: main leader reads the request and freezes execution-relevant semantics.
- `l2_advisory`: optional advisory/planning route when the task needs sharpening.
- `l3_bridge`: bridge/preflight/documentation-oriented route.
- `l4_implement`: implementation-facing work.
- `l4_execute`: validation, execution, and post-run checks.
- `l4_anomaly`: recovery path for failed, partial, blocked, or orphaned windows.

From `l3_bridge`, the graph intentionally allows `l3_bridge -> l3_bridge` and `l3_bridge -> leader_freeze`. This covers the common loop where L3 inspects repo/document state, asks for user confirmation, then resumes the same L3 documentation or preflight task before moving to L4.

L3 packets have a documentation responsibility. When the work touches docs, Markdown, `CLAUDE.md`, README, setup/usage guidance, workflow rules, or agent behavior, L3 must explicitly decide whether repo-facing documentation needs a bounded update. `CLAUDE.md` is a first-class L3 target for workflow and agent-behavior changes.

L3 packets also carry a minimum-active-surface responsibility. Curator should first understand the current step, what prior work has already completed, and what the next phase actually needs; then it should archive stale, duplicate, ambiguous, or non-current logs, datasets, checkpoints, generated outputs, stale code copies, scratch scripts, and misleading inactive documents out of active reach. Archive is the default for material with possible audit value. Physical deletion is reserved for clearly disposable material or explicit approval.

L4 implement inherits that hygiene requirement. Implementors should modify existing files when practical, use temporary scripts for one-off work, create long-lived files only for durable need, and avoid handing rungater/executor an active surface cluttered with exploratory logs, scratch scripts, stale checkpoints, duplicate code copies, or stale data.

L4 execute is intentionally different from short implementation or review windows. It may own long-running training or evaluation jobs. For L4 execute, `TeamIdle` means the team is waiting or polling; it is not completion and is not a reason to delete the team. If executor launches an owned long-running process, the bridge window should remain open until the process reaches a terminal state and postrun has audited terminal logs/artifacts.

The phase is not just a final label. It is a runtime trace of important action intent, action start, action end, denial, failure, partial completion, and orphaning. This allows later audit to distinguish "never attempted" from "attempted and failed" from "started but never returned".

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

Task specs preserve compound user intent instead of reducing it to one short description. The packet includes the original instruction, an `instruction_coverage_checklist`, and preserved context fields. Teammate assignments must report whether each checklist item was completed, deferred with a concrete reason, blocked, or escalated. This prevents a multi-part user request from being half-executed and then treated as complete.

## Runtime And Ledgers

Runtime state is stored under the parent `.claude` tree:

```text
.claude/runtime_state/projects/<repo-key>/runs/
```

The repo key is derived from the target repo path, so multiple sibling repos can share the same parent control plane while keeping separate run ledgers.

Per run, the runtime writes:

- `run_ledger.json`: authoritative mutable run state
- `runtime_snapshot.json`: current truth for leaders and tools
- `event_log.jsonl`: raw workflow events
- `check_ledger.jsonl`: check decisions and reasons
- `update_ledger.jsonl`: persisted update results
- `transitions.jsonl`: lifecycle transition facts
- `main_leader_inbox.jsonl`: notifications for the main leader

The runtime also writes read-only Bridge Companion observer streams. These are not authoritative workflow state; they are structured side-channel facts for UI/debug display:

- `bridge_packets.jsonl`: packet summary, user instruction, scope, team, completion/report contract
- `agent_messages.jsonl`: bridge-leader to teammate assignment messages and checklist coverage refs
- `tool_events.jsonl`: tool starts/completions, safe input previews, file refs, output summary, duration when available
- `session_bindings.jsonl`: session-to-run/team/task/teammate binding facts for UI attribution
- `session_events.jsonl`: safe session-level previews for prompts, tool starts/completions, stops, and session end
- `teammate_reports.jsonl`: structured progress, completed/open/blocked items, evidence refs, file refs
- `process_events.jsonl`: long-running process refs, PID/state/heartbeat/log/artifact probe
- `artifacts.jsonl`: artifact references recorded from runtime events
- `completion_checks.jsonl`: completion/checklist item disposition
- `companion_events.jsonl`: merged observer stream with source backrefs

Tool observer records are emitted for all Claude Code sessions, not only bridge child sessions. Records include `session_kind`, `run_binding_state`, `session_id`, run/window/team/task IDs when available, `teammate_id`, `agent_type`, `tool_name`, `tool_use_id`, and `status`. If a hook cannot bind a tool event to a run, it writes the safe preview to `.claude/runtime_state/session_observer/` so Companion can still show direct or unbound session activity.

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

If L3 needs user confirmation, the main leader should ask the user, record the answer, and continue the same L3 task through the legal `l3_bridge -> l3_bridge` or `l3_bridge -> leader_freeze` path. The user should not need to manually say "reroute"; bridge-denial notifications include a recommended legal next route when one exists.

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

Claude Code must load both parent-level configuration files:

- `../.claude/settings.json` for agent, environment, and hooks
- `../.claude/mcp.json` for the bridge MCP server

Start from inside the target repo:

```powershell
cd C:\path\to\workspace-parent\your-repo
claude --settings ../.claude/settings.json --mcp-config ../.claude/mcp.json --strict-mcp-config
```

Use a wrapper if desired:

```powershell
function cc {
  claude --settings ../.claude/settings.json --mcp-config ../.claude/mcp.json --strict-mcp-config @args
}
```

`--strict-mcp-config` is recommended so Claude does not silently mix in repo-local MCP config.

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
.claude/control/runtime/main_leader.py
.claude/control/runtime/bridge_sdk.py
.claude/control/runtime/bridge_leader.py
.claude/control/runtime/claude_cli_executor.py
.claude/control/runtime/companion_observer.py
.claude/hooks/*.py                        Claude hook adapters
.claude/control/policy/*.json             Lifecycle, phase, approval policy
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

Without `BRIDGE_EXECUTOR=simulate`, the bridge executor uses a nested non-interactive Claude Code call through `claude -p`.

## Notes

- No user-level install is required.
- No `~/.claude` changes are required.
- No repo-local workflow code, `.claude/` directory, or `.mcp.json` file is required.
- The only required shared artifact is the sibling parent `.claude` directory.
- The startup command or wrapper must pass both `--settings ../.claude/settings.json` and `--mcp-config ../.claude/mcp.json`.
