---
name: cc-refresher
description: Invoke the Claude-owned run-spec initialization and refresh role.
---

# cc-refresher

This skill invokes the Claude-owned Refresher role through the shared Claude bridge runner.

## When to use
- When the orchestrator needs execution-facing run-state documentation initialized or refreshed
- When current-run spec state is stale, missing, or needs to be rewritten from the compiled task basis
- When downstream execution needs a clean current-run source of truth before Curator / Preflight / Opus continue

## Inputs
- repository root
- refresher packet
- optional phase
- optional explicit output directory
- optional explicit run id

## Outputs
- raw bridge request/response
- worker textual output
- runner-written report, handoff, receipt, orchestrator summary, and any extra artifacts returned by the worker payload

## Carrier rule
This `cc-*` skill is the Claude leaf-worker entrypoint for the Refresher role.
Do not assume a separate `~/.claude/agents/` role registry exists.
Use `bin/run.sh` to invoke the shared Claude bridge runner.