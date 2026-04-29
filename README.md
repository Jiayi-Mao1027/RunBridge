# Parent-Sibling Claude Workflow

This project provides a portable Claude Code workflow control plane that lives in a single `.claude` directory.

It is designed for this layout:

```text
workspace-parent/
  .claude/
  your-repo/
    .mcp.json
```

Run Claude Code from inside `your-repo/`. The workflow files stay outside the repo and beside it, so several repos under the same parent can share the same control logic without copying runtime code into each repo.

## Startup

Claude Code must load the sibling `.claude/settings.json`. The repo under test should contain a project `.mcp.json` that points back to the sibling bridge server.

If your Claude Code build does not automatically discover the sibling settings file, use an alias or wrapper that passes it explicitly:

```powershell
Set-Alias cc 'claude'
# Example direct form:
claude --settings ../.claude/settings.json
```

Use the alias from inside the repo:

```powershell
cd C:\path\to\workspace-parent\your-repo
claude --settings ../.claude/settings.json
```

The workflow assumes the current working directory is the repo root. Relative paths in settings use `../.claude/...`.

## Key Configuration

Main configuration lives in:

```text
.claude/settings.json
.claude/mcp.json
your-repo/.mcp.json
```

It defines:

- environment defaults: `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`
- the default front-facing session agent: `leader-orchestrator`
- the `bridge` MCP server in `your-repo/.mcp.json`:

```text
python ../.claude/control/mcp/bridge_server.py
```

- hooks pointing back to:

```text
../.claude/hooks/*.py
```

The bridge MCP exposes:

- `mcp__bridge__read_runtime_snapshot`
- `mcp__bridge__build_bridge_packet`
- `mcp__bridge__call_bridge_sdk`
- `mcp__bridge__dispatch_workflow_event`
- `mcp__bridge__reconcile_workflow_from_ledger`

## Runtime State

Runtime state is stored inside the same sibling `.claude` tree, separated per repo:

```text
workspace-parent/.claude/runtime_state/projects/<repo-key>/runs
```

The repo key is derived from the repo path, so multiple sibling repos can share the control plane while keeping separate ledgers.

Per run, the runtime writes:

- `run_ledger.json`
- `runtime_snapshot.json`
- `event_log.jsonl`
- `check_ledger.jsonl`
- `update_ledger.jsonl`
- `transitions.jsonl`
- `main_leader_inbox.jsonl`

## Workflow

Normal flow:

```text
main-leader
  -> build one BridgePacket
  -> call bridge SDK through MCP
  -> bridge-leader accepts one bridge window
  -> one team + one task
  -> teammate execution
  -> completion evidence
  -> bridge result returns to main-leader
```

One bridge window binds exactly one team and one task. The task may have multiple teammate assignments, but it must not describe multiple independent tasks.

## Important Files

```text
.claude/CLAUDE.md                         Main operating contract
.claude/settings.json                     Agent, environment, and hook configuration
.claude/mcp.json                          MCP server template for copied project config
your-repo/.mcp.json                       Active project MCP server configuration
.claude/agents/leader-orchestrator.md     Main leader instructions
.claude/agents/bridge-leader.md           Bridge-window owner instructions
.claude/control/mcp/bridge_server.py      MCP bridge server
.claude/control/runtime/main.py           CLI runtime entry point
.claude/control/runtime/workflow_runtime.py
.claude/control/runtime/bridge_sdk.py
.claude/control/runtime/bridge_leader.py
.claude/hooks/*.py                        Claude hook adapters
.claude/control/policy/*.json             Lifecycle, phase, approval policy
.claude/control/schemas/*.json            Runtime data contracts
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
- No repo-local workflow code is required beyond the project MCP pointer file.
- The only required shared artifact is the sibling `.claude` directory.
- If Claude Code does not auto-load sibling settings, the startup alias must pass `--settings ../.claude/settings.json`.
