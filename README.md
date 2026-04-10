# Claude Code Agent System

This directory is the user-level home of the current **Claude Code-centered agent system**.

The live system is built around:
- `/data03/liang/mjy/.claude`

The older Codex-centered stack is still present on disk for migration reference, but it is no longer the primary live control system.

## What The Current System Is

This is a controlled Claude Code runtime with:
- a single active isolated control surface
- hook-driven lifecycle enforcement
- durable machine-readable runtime state
- thin protocol objects instead of long handoff prose
- project-local semantics owned by each repository

The system is not meant to be a free-form agent sandbox.

Its purpose is to keep work:
- resumable
- auditable
- mechanically checkable at lifecycle boundaries
- explicit about task, teammate, and completion state

## Active Control Surface

The active control root is:
- `/data03/liang/mjy/.claude`

The main live files there are:
- `/data03/liang/mjy/.claude/settings.json`
  - live Claude Code settings
- `/data03/liang/mjy/.claude/CLAUDE.md`
  - current control notes and hook/protocol boundaries
- `/data03/liang/mjy/.claude/hooks/`
  - executable lifecycle hooks
- `/data03/liang/mjy/.claude/runtime_state/`
  - durable hook-written state
- `/data03/liang/mjy/.claude/backups/`
  - backups of replaced control files

This is the directory that should be treated as the current system of record for user-level Claude Code behavior.

## Core Model

The current design is intentionally thin.

Hooks are responsible for:
- boundary enforcement
- task/team lifecycle gating
- lightweight validation
- event logging
- checkpoint persistence

Hooks are not responsible for:
- long-form planning
- semantic negotiation with the user
- replacing real implementation or review judgment
- recreating the old handoff-as-essay workflow

The intended split is:
- hooks manage edges
- durable artifacts carry state
- agents or teammates do the actual thinking and work

## Active Protocol Objects

The current durable protocol uses four main objects.

### 1. Task Envelope

Written on `TaskCreated`.

Purpose:
- freeze minimum task identity
- record task/team/layer metadata
- capture declared required outputs when present

Stored under:
- `/data03/liang/mjy/.claude/runtime_state/projects/<project-key>/task_envelopes/`

### 2. Teammate Status

Written on `TeammateIdle`.

Purpose:
- record idle-ready teammate state
- preserve minimal handoff information
- keep partial/final/blocked teammate states durable

Stored under:
- `/data03/liang/mjy/.claude/runtime_state/projects/<project-key>/teammate_status/`

### 3. Completion Receipt

Written on `TaskCompleted`.

Purpose:
- record task closure
- validate declared outputs when they exist
- preserve final task status and handoff metadata

Stored under:
- `/data03/liang/mjy/.claude/runtime_state/projects/<project-key>/completion_receipts/`

### 4. Session Checkpoint

Written on `SessionEnd`, then consumed by `SessionStart`.

Purpose:
- preserve resumable session state
- record open tasks, recent completions, owned processes, and recent events

Stored under:
- `/data03/liang/mjy/.claude/runtime_state/checkpoint.json`
- `/data03/liang/mjy/.claude/runtime_state/session_start_last.json`

## Hook Chain

The current live hook set is:

- `SessionStart`
  - `hooks/session_start_summary.py`
  - captures start/resume context and attaches the latest checkpoint
- `PreToolUse:Bash`
  - `hooks/pre_bash_guard.py`
  - blocks unsafe kill patterns and destructive commands on critical paths
- `PostToolUse:Bash`
  - `hooks/post_bash_check.py`
  - records GPU probe state, background PIDs, and non-zero exits
- `TaskCreated`
  - `hooks/task_created.py`
  - writes the minimal task envelope
- `TeammateIdle`
  - `hooks/teammate_idle.py`
  - writes teammate status and blocks obviously incomplete final idle states
- `TaskCompleted`
  - `hooks/task_completed.py`
  - writes completion receipts and validates declared outputs
- `PreToolUse:Edit|Write`
  - `hooks/pre_edit_guard.py`
  - protects sensitive edit targets
- `PostToolUse:Edit|Write`
  - `hooks/post_edit_check.py`
  - records touched files and performs lightweight JSON validation
- `Stop`
  - configured as an agent hook in `settings.json`
  - audits whether a response ended at a valid stage boundary
- `SessionEnd`
  - `hooks/session_end_checkpoint.py`
  - writes the durable exit checkpoint

## Runtime State

Important durable state currently lives under:
- `/data03/liang/mjy/.claude/runtime_state/`

Notable files include:
- `event_log.jsonl`
- `process_guard/owned.json`
- `gpu_probed`
- `session_start_last.json`
- `checkpoint.json`

The key rule is:
- important execution and lifecycle state should be recoverable from files, not only from chat text

## Project Boundary

The user-level agent system does not own project semantics.

Project repositories such as:
- `/data03/liang/mjy/safe_opd`
- `/data03/liang/mjy/ntk`

own:
- code
- specs
- run artifacts
- project-local `CLAUDE.md` guidance

The user-level Claude Code system under `.claude` owns:
- user-level Claude settings
- hook behavior
- durable control/runtime state

## Legacy Components

These older directories still exist:
- `/data03/liang/mjy/.codex`
- `/data03/liang/mjy/.agents`

They are no longer the primary live control plane.
Treat them as:
- historical reference
- migration source material
- helper logic source when porting old safeguards

Do not describe them as the active front-door agent system unless you intentionally switch the system back.

There is also:
- `/data03/liang/mjy/.claude`

For this setup, `.claude` is the active Claude Code system.

## Maintenance Rules

When updating the current agent system:
- keep `/data03/liang/mjy/.claude/settings.json` aligned with actual hook locations
- keep `/data03/liang/mjy/.claude/CLAUDE.md` aligned with actual runtime behavior
- keep `/data03/liang/mjy/.claude/hooks/` as the executable source for hook logic
- prefer durable runtime state over chat-only memory
- back up replaced control files before large control-surface edits

## Reading Order

If you need to understand the current live agent system, read in this order:
1. `/data03/liang/mjy/.claude/settings.json`
2. `/data03/liang/mjy/.claude/CLAUDE.md`
3. `/data03/liang/mjy/.claude/hooks/`
4. the target project repository's `CLAUDE.md` and specs

If you need the pre-migration system for comparison, then inspect:
1. `/data03/liang/mjy/.codex`
2. `/data03/liang/mjy/.agents`
