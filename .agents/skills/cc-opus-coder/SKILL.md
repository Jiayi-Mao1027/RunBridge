---
name: cc-opus-coder
description: Invoke the Claude-owned primary execution role for implement/debug/execute, with explicit GPU and process-safety rules.
---

# cc-opus-coder

This skill invokes the Claude-owned Opus Coder role through the shared Claude bridge runner.

## When to use
- When the orchestrator wants approved execution work implemented
- When bounded debug loops are needed
- When formal execution is approved and must be carried out under execution-manifest discipline

## Inputs
- repository root
- opus packet
- optional phase
- optional explicit output directory
- optional explicit run id

## Outputs
- raw bridge request/response
- worker textual output
- runner-written report, handoff, receipt, orchestrator summary, and any extra artifacts returned by the worker payload

## Carrier rule
This `cc-*` skill is the Claude leaf-worker entrypoint for the Opus Coder role.
Do not assume a separate `~/.claude/agents/` role registry exists.
Use `bin/run.sh` to invoke the shared Claude bridge runner.