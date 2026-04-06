# Codex Agent System

This directory is the user-level home of the live Codex-centered agent system.

The active system is shared across projects and lives under:
- `/data03/liang/mjy/.codex`
- `/data03/liang/mjy/.agents`

Project repositories such as `/data03/liang/mjy/safe_opd` provide project semantics, code, specs, and run artifacts. They are not the source of truth for the orchestration runtime.

## What This System Is

This is a controlled execution architecture with:
- a single Codex Orchestrator as the control plane
- fixed downstream worker roles
- Claude-backed leaf workers invoked through Codex skills
- durable file-backed runtime state instead of chat-memory-only state

The system is not a free-form multi-agent sandbox.

Its purpose is to make runs:
- auditable
- reconstructible
- role-separated
- stage-aware
- explicit about ownership, defaults, and unresolved items

## Control Model

The authoritative control contract is:
- `/data03/liang/mjy/.codex/AGENTS.md`

Layer model:
- Layer 1: the user
- Layer 2: the Codex Orchestrator
- Layer 3: execution and audit workers

The Codex Orchestrator is the only front-facing controller. It owns:
- freezing run semantics
- typing unresolved items
- approving or rejecting change-set expansion
- routing stages and workers
- synthesizing final reporting back to the user

Claude is not a control plane. Claude-backed roles are external leaf workers invoked only through `cc-*` skills.

## Roles

Codex-owned roles:
- Orchestrator
- Preflight Auditor
- Postrun Auditor
- Codex Anomaly Analyst

Claude-owned leaf workers:
- Refresher
- Curator
- Opus Coder
- Claude Anomaly Analyst

Claude-owned workers must run through skills in:
- `/data03/liang/mjy/.agents/skills/`

Important entrypoints include:
- `/data03/liang/mjy/.agents/skills/cc-refresher/bin/run.sh`
- `/data03/liang/mjy/.agents/skills/cc-curator/bin/run.sh`
- `/data03/liang/mjy/.agents/skills/cc-opus-coder/bin/run.sh`
- `/data03/liang/mjy/.agents/skills/cc-claude-anomaly-analyst/bin/run.sh`

## Directory Layout

`/data03/liang/mjy/.codex`
- `AGENTS.md`
  - control-plane constitution
- `config.toml`
  - Codex runtime configuration
- `hooks.json`
  - hook registration
- `bridge_api.toml`
  - shared Claude bridge configuration
- `agents/`
  - Codex-owned role configs
- `protocol/bin/`
  - bridge runner, process guard, hook scripts, runtime helpers
- `protocol/runtime/`
  - runtime policy such as GPU policy
- `runtime_state/`
  - durable user-level control-plane state

`/data03/liang/mjy/.agents`
- `skills/`
  - Claude worker skills, worker contracts, shell wrappers, helper scripts

## Authoritative Paths

In this deployment:
- `~/.codex` resolves to `/data03/liang/mjy/.codex`
- `~/.agents` resolves to `/data03/liang/mjy/.agents`

Live control-plane code and runtime state must not silently fall back to:
- `/root/.codex`

One intentional exception remains:
- the external Claude CLI binary currently lives at `/root/.local/bin/claude`

That binary path is not control-plane state. The system state, hooks, bridge logic, and worker contracts are all under `/data03/liang/mjy/.codex` and `/data03/liang/mjy/.agents`.

## Claude Bridge

The shared Claude bridge config is:
- `/data03/liang/mjy/.codex/bridge_api.toml`

Current live bridge properties:
- `cli_path = "/root/.local/bin/claude"`
- forced settings file:
  - `/data03/liang/mjy/.claude-isolated/settings.json`
- canonical model naming:
  - `claude-sonnet-4-6`
- `--bare` mode is forced by the runner in this deployment
- run-local Claude debug logs are written when workers run

The shared runner is:
- `/data03/liang/mjy/.codex/protocol/bin/claude_skill_runner.py`

The runner is responsible for:
- reading the user-level constitution
- assembling worker packets
- forcing the settings path
- normalizing bad compact model aliases
- writing output artifacts
- preserving bridge/provider failure evidence in run artifacts

## Hooks and Runtime Guards

Hooks are registered in:
- `/data03/liang/mjy/.codex/hooks.json`

Current hook chain:
- `SessionStart`
  - `/data03/liang/mjy/.codex/protocol/bin/session_start_summary.py`
- `PreToolUse` for Bash
  - `/data03/liang/mjy/.codex/protocol/bin/pre_bash_guard.py`
- `PostToolUse` for Bash
  - `/data03/liang/mjy/.codex/protocol/bin/post_bash_check.py`
- `Stop`
  - `/data03/liang/mjy/.codex/protocol/bin/stop_checkpoint.py`

Runtime/process helpers:
- `/data03/liang/mjy/.codex/protocol/bin/owned_processes.py`
- `/data03/liang/mjy/.codex/protocol/bin/gpu_probe.py`
- `/data03/liang/mjy/.codex/protocol/runtime/gpu_policy.toml`

Current guard behavior includes:
- blocking destructive commands on critical paths without explicit authorization
- forbidding pattern-based process termination such as `pkill` and `killall`
- allowing `kill` only for explicit numeric PIDs in the current owned execution stack
- requiring GPU probing before heavy GPU launches
- tracking wrapper PID, runner PID, and descendant processes as one owned execution action

## Run Artifacts

The preferred durable run layout is project-local:
- `artifacts/runs/<run_id>/`

Expected contents may include:
- packets
- handoffs
- reports
- completion receipts
- manifests
- per-run outputs
- debug evidence
- reusable evaluation artifacts

Repo-global `logs/` are not the authoritative experiment memory unless a project explicitly says so.

If an output may be reused later, keep it traceable to its source run directory rather than flattening it into a shared log pile.

## Standard Run Cycle

The canonical run flow is:
1. user instruction
2. orchestrator freezes meaning and execution plan
3. Refresher refreshes run-state docs when needed
4. Curator performs hygiene review when needed
5. Preflight runs in `initial_readiness`
6. Opus performs `implement`
7. Opus performs `debug`
8. Preflight runs in `run_gate`
9. Opus performs formal `execute`
10. Postrun audits the result
11. anomaly analysis runs on two independent routes when needed
12. orchestrator synthesizes and reports upward

Not every run uses every stage, but any omitted stage should be intentionally omitted.

## Bootstrap and First Run

When a project repo is missing bootstrap folders, use:
- `/data03/liang/mjy/.agents/skills/ensure-project-state/SKILL.md`

That skill is only for bootstrap state such as:
- `specs/`
- `artifacts/`
- `artifacts/runs/`

It is not a semantic planner and does not replace Refresher.

## Operational Rules

- Keep the control plane user-level, not repo-local, unless explicitly re-scoped.
- Treat `/data03/liang/mjy/.codex/AGENTS.md` as the constitution.
- Treat project-level `AGENTS.md`, `CLAUDE.md`, and `specs/` as project semantics.
- Do not silently expand the approved change set.
- Prefer durable files over chat memory.
- Keep worker outputs and debug evidence under `artifacts/runs/<run_id>/`.
- Do not kill processes that were not started by the current owned execution action.
- When GPU memory is occupied by foreign idle jobs, do not kill them by default; colocate only when headroom is sufficient and record that choice in artifacts.

## Verified State

The system has been smoke-tested recently on an isolated test repo.

Verified working:
- shared Claude bridge path
- Refresher
- Curator
- Opus Coder
- Claude Anomaly Analyst
- Preflight Auditor
- Postrun Auditor
- Codex Anomaly Analyst
- user-level hook pathing
- owned-process tracking and kill guard

Important verified behavior:
- all live control-plane runtime state resolves under `/data03/liang/mjy/.codex`
- worker wrappers are executable
- the bridge uses the forced isolated settings file
- canonical model naming is required for the provider bridge
- worker failures preserve run-local debug evidence

## Troubleshooting

If a new session behaves oddly, check these first:
- `/data03/liang/mjy/.codex/AGENTS.md`
- `/data03/liang/mjy/.codex/config.toml`
- `/data03/liang/mjy/.codex/hooks.json`
- `/data03/liang/mjy/.codex/bridge_api.toml`
- `/data03/liang/mjy/.codex/runtime_state/`

If a Claude worker fails, inspect:
- the role output directory under `artifacts/runs/<run_id>/...`
- `protocol_error.md`
- the raw Claude debug log written by the shared runner

If process termination is denied unexpectedly, inspect:
- `/data03/liang/mjy/.codex/runtime_state/process_guard/owned.json`
- `/data03/liang/mjy/.codex/protocol/bin/owned_processes.py`
- `/data03/liang/mjy/.codex/protocol/bin/pre_bash_guard.py`

## Scope of This README

This README explains the shared agent system itself.

It is not:
- a project research plan
- a project mission note
- a changelog
- a substitute for project-level specs

Project-specific meaning belongs in the project repositories.
