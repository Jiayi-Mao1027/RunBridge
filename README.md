# Codex Control Plane

This repository root is the user-level home for the live Codex-centered agent system.

The active control-plane implementation lives here:
- `.codex/`
- `.agents/`

Project repositories consume that shared control plane.
They are not supposed to redefine the runtime root or silently fork the orchestration logic into repo-local clones.

## What Is Canonical

The canonical user-level control assets are:
- `.codex/AGENTS.md`
  - control-plane constitution
- `.codex/config.toml`
  - Codex runtime configuration
- `.codex/hooks.json`
  - hook registration
- `.codex/protocol/`
  - shared protocol scripts, runtime policy, schemas, and templates
- `.codex/agents/`
  - Codex-owned auditor / anomaly role definitions
- `.agents/skills/`
  - Claude-owned leaf-worker entrypoints and contracts

The live system should resolve against `/data03/liang/mjy/.codex` and `/data03/liang/mjy/.agents`, not `/root/.codex`.

## Architectural Direction

The system is Codex-centered:
- Codex Orchestrator is the control plane
- Claude-owned roles are invoked only through `cc-*` skills
- durable run state should live in files, not only in chat memory

Shared runtime logic belongs at the user level.
Project repositories should provide:
- project semantics
- run specs
- run artifacts
- code and experiment logic

They should not carry abandoned copies of the control-plane runtime as if those copies were still authoritative.

## Run State

The preferred durable run structure is:
- `artifacts/runs/<run_id>/`
  - packets
  - reports
  - handoffs
  - completion receipts
  - manifests
  - per-run outputs
  - reusable evaluation artifacts

Repo-global `logs/` directories are secondary or legacy compatibility paths unless a project spec explicitly says otherwise.

When prior experiment outputs are reused, they should remain traceable to their source run directory rather than being flattened into an ambiguous shared log pile.

## Hooks and Guards

The user-level protocol currently provides:
- session start checkpoint capture
- pre-bash safety guard
- post-bash state tracking
- stop checkpoint persistence
- process ownership tracking
- GPU policy helpers

These hooks are meant to support:
- durable runtime state
- safer execution
- explicit process ownership
- reproducible run evidence

## Current Practical Rules

- If a shell wrapper is an entrypoint, it should be executable.
- If a worker needs settings, the shared runner should inject them in one canonical way.
- If a role consumes old experiment evidence, prefer tagged run-local artifacts over unscoped shared logs.
- If a repo contains an older local orchestration scaffold, treat the shared user-level control plane as authoritative unless explicitly re-scoped.

## Intended Usage

1. Enter a project repository.
2. Let the shared `.codex` control plane interpret the task.
3. Freeze run semantics in project specs and run artifacts.
4. Dispatch Claude-owned leaf workers through `.agents/skills/cc-*`.
5. Keep outputs reconstructible under `artifacts/runs/<run_id>/`.

This root README is only a user-level control-plane orientation note.
Project-specific research meaning belongs in the project repositories, not here.
