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
- `curator`: clarifies artifact/log/dataset/checkpoint/output boundaries and traceability.
- `refresher`: performs bounded documentation refresh when the packet allows it.
- `implementor`: makes approved code/config changes inside the target repo boundary.
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

Generated runtime folders such as `.claude/runtime_state/` and `.claude/worktrees/` can be cleared when no workflow is running, but doing so deletes historical debug/replay evidence.

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
.claude/hooks/*.py                        Claude hook adapters
.claude/control/policy/*.json             Lifecycle, phase, approval policy
.claude/control/schemas/*.json            Runtime data contracts
pseudocode/main_workflow.md               High-level intended workflow
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
