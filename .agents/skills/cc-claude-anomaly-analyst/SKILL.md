---
name: cc-claude-anomaly-analyst
description: Invoke the Claude-owned independent anomaly route after postrun recommends anomaly analysis.
---

# cc-claude-anomaly-analyst

This skill invokes the Claude anomaly route through the shared Claude bridge runner.

## When to use
- After postrun recommends anomaly analysis
- When the Claude route should run independently from the Codex route
- When the orchestrator wants an evidence-backed second anomaly path before synthesis

## Inputs
- repository root
- route-specific anomaly packet
- optional phase
- optional explicit output directory

## Outputs
- raw bridge request/response
- leaf-worker textual output
- downstream protocol artifacts produced by repository helpers or orchestrator-side postprocessing

## Carrier rule
This `cc-*` skill is the actual Claude leaf-worker entrypoint for the Claude Anomaly Analyst role.
Do not assume a separate `~/.claude/agents/` role registry exists.
Use `bin/run.sh` to invoke the shared Claude bridge runner.