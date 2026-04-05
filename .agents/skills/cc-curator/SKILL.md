---
name: cc-curator
description: Invoke the Claude-owned Curator role for workspace hygiene, retention, archive/delete/promote decisions, and hygiene reporting.
---

# cc-curator

This skill invokes the Claude-owned Curator role through the shared Claude bridge runner.

## When to use
- After refresher, when workspace hygiene or asset-organization decisions are needed
- When the orchestrator wants a structured retain/archive/delete/promote judgment
- When hygiene findings must be separated into true blockers versus execution-layer work

## Inputs
- repository root
- curator packet
- optional phase
- optional explicit output directory

## Outputs
- raw bridge request/response
- worker textual output
- runner-written report, handoff, receipt, and orchestrator summary artifacts

## Carrier rule
This `cc-*` skill is the Claude leaf-worker entrypoint for the Curator role.
Do not assume a separate `~/.claude/agents/` role registry exists.
Use `bin/run.sh` to invoke the shared Claude bridge runner.