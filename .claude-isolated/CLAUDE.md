# Claude Code Control Notes

This directory is the active Claude Code control surface.

The old role-heavy agent system is no longer the primary source of truth here.
Legacy files under `agents/` may still exist, but the active behavior is defined by:

1. `settings.json`
2. `hooks/`
3. `runtime_state/`

The goal is a thin, mechanical, hook-driven control layer:

- hooks enforce boundaries
- hooks record durable state
- hooks gate task and teammate lifecycle edges
- hooks do not replace planning, judgment, or execution quality

## Active Protocol Objects

The active durable protocol is intentionally small.

### 1. Task Envelope

Written on `TaskCreated`.

Purpose:
- freeze the minimum task identity
- record task/team/layer metadata
- capture declared required outputs when present

Stored under:
- `runtime_state/projects/<project-key>/task_envelopes/`

### 2. Teammate Status

Written on `TeammateIdle`.

Purpose:
- record whether a teammate is idling with partial work, final work, or a blocked state
- preserve a minimal handoff surface

Stored under:
- `runtime_state/projects/<project-key>/teammate_status/`

### 3. Completion Receipt

Written on `TaskCompleted`.

Purpose:
- record task closure
- validate required outputs when the task declared them
- capture final status and handoff metadata when present

Stored under:
- `runtime_state/projects/<project-key>/completion_receipts/`

### 4. Session Checkpoint

Written on `SessionEnd`.

Purpose:
- preserve resumable state
- record open tasks, recently completed tasks, owned processes, and recent events

Stored under:
- `runtime_state/checkpoint.json`
- `runtime_state/session_start_last.json`

## Hook Map

### SessionStart

Script:
- `hooks/session_start_summary.py`

Responsibility:
- capture the incoming session payload
- attach the latest checkpoint when available
- write a short resumable session-start record

This hook is non-blocking.

### PreToolUse:Bash

Script:
- `hooks/pre_bash_guard.py`

Responsibility:
- deny unsafe kill patterns
- deny destructive operations on critical paths
- warn when GPU launches happen before a probe in the current session

This hook is mechanical.
It is not a planner or reviewer.

### PostToolUse:Bash

Script:
- `hooks/post_bash_check.py`

Responsibility:
- record GPU probe timestamps
- register owned background PIDs detected from command output
- log non-zero bash exits

This hook records telemetry.
It does not approve or reject semantic decisions after the fact.

### TaskCreated

Script:
- `hooks/task_created.py`

Responsibility:
- create a minimal task envelope
- infer a layer when it is not declared
- detect obvious task-id collisions

This hook should stay thin.
It is not the place for long planning prose.

### TeammateIdle

Script:
- `hooks/teammate_idle.py`

Responsibility:
- write a minimal teammate status
- block a teammate from idling if it claims a final result without a completion receipt
- block a teammate from idling with open issues but no resume hint

This replaces bloated handoff-writing with a minimal durable status object.

### TaskCompleted

Script:
- `hooks/task_completed.py`

Responsibility:
- write a completion receipt
- validate declared required outputs when they exist
- prevent obvious completion-state regressions

### PreToolUse:Edit|Write

Script:
- `hooks/pre_edit_guard.py`

Responsibility:
- deny edits to protected paths
- allow edits inside the active project and inside this `.claude-isolated` control surface

### PostToolUse:Edit|Write

Script:
- `hooks/post_edit_check.py`

Responsibility:
- record touched files
- run lightweight JSON syntax validation after edits

### Stop

Configured as an agent hook in `settings.json`.

Responsibility:
- audit whether the current response reached a valid stage boundary
- check for missing receipts or missing carry-forward state

`Stop` is a turn-boundary audit.
It is not the durable exit checkpoint.

### SessionEnd

Script:
- `hooks/session_end_checkpoint.py`

Responsibility:
- write the durable exit checkpoint
- snapshot open tasks, recent completions, and owned-process state

`SessionEnd` is the real session-exit persistence point.

## Runtime State

Runtime state is written under:
- `runtime_state/`

Important files include:
- `runtime_state/event_log.jsonl`
- `runtime_state/process_guard/owned.json`
- `runtime_state/gpu_probed`
- `runtime_state/session_start_last.json`
- `runtime_state/checkpoint.json`

This state is durable and machine-readable.
Conversation text alone is not sufficient when a task or session needs to be resumed or audited.

## Design Boundaries

Hooks should do:
- admission checks
- boundary checks
- lightweight validation
- event logging
- durable checkpointing

Hooks should not do:
- long-form planning
- semantic negotiation with the user
- heavy research
- replacing execution or review judgment
- recreating the old handoff-as-essay workflow

The intended design is:

- hooks manage edges
- durable artifacts carry state
- agents or teammates do the actual thinking and work

