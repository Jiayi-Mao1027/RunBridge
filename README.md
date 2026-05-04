# Parent-Sibling Claude Workflow

This project provides a portable Claude Code workflow control plane that lives in a single `.claude` directory.

It is designed for this layout:

```text
workspace-parent/
  .claude/
  your-repo/
```

Run Claude Code from inside `your-repo/`. The workflow files stay outside the repo and beside it, so several repos under the same parent can share the same control logic without copying runtime code or MCP configuration into each repo.

## Startup

Claude Code must load both sibling configuration files:

- `../.claude/settings.json` for agent, environment, and hooks
- `../.claude/mcp.json` for the `bridge` MCP server

Start from inside the repo:

```powershell
cd C:\path\to\workspace-parent\your-repo
claude --settings ../.claude/settings.json --mcp-config ../.claude/mcp.json --strict-mcp-config
```

Use a wrapper or shell function if you do not want to type the flags every time:

```powershell
function cc {
  claude --settings ../.claude/settings.json --mcp-config ../.claude/mcp.json --strict-mcp-config @args
}
```

The workflow assumes the current working directory is the repo root. The repo under test does not need a local `.claude/` directory or `.mcp.json` file.

## Key Configuration

Main configuration lives in:

```text
.claude/settings.json
.claude/mcp.json
```

It defines:

- environment defaults: `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`
- the default front-facing session agent: `leader-orchestrator`
- the `bridge` MCP server in `.claude/mcp.json`:

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
.claude/mcp.json                          Active bridge MCP server configuration
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
- No repo-local workflow code, `.claude/` directory, or `.mcp.json` file is required.
- The only required shared artifact is the sibling `.claude` directory.
- The startup command or wrapper must pass both `--settings ../.claude/settings.json` and `--mcp-config ../.claude/mcp.json`.
