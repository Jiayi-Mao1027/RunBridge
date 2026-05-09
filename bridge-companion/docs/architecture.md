# Bridge Companion Architecture

Bridge Companion is outside the Bridge Runtime control loop. It is a read-only observation layer with five responsibilities:

- observe runtime streams
- attribute activity to repo, run, bridge window, team, task, teammate, and session
- present current activity and history
- recover after disconnects by backfilling from JSONL files
- jump to raw runtime facts for audit

It is forbidden to:

- create bridge windows
- create teams or tasks
- write ledgers or snapshots
- modify frozen semantics
- control retry or routing
- decide task completion
- inject context into system agents

## Data Flow

```text
.claude/runtime_state/projects/<repo-key>/runs/<run-id>/*.jsonl
  -> gateway source readers
  -> normalized CompanionEvent
  -> REST backfill and SSE live stream
  -> UI reducer
  -> team tree, activity stream, detail inspector, trajectory tab
```

Global unbound or direct-session activity is read from:

```text
.claude/runtime_state/session_observer/*.jsonl
```

The gateway never calls Bridge MCP tools and never writes runtime files.

## Multi-Repo Layout

The gateway discovers repositories from:

```text
.claude/runtime_state/projects/
```

Each child directory is treated as one `repoKey`; each `repoKey/runs/<run-id>` directory is one run.

`latest` is calculated per repo by sorting runs on snapshot, ledger, or directory mtime. It is not global across all repos.

## Normalized Event Sources

| Source file | Companion source | Display rule |
| --- | --- | --- |
| `sdk_stream_events.jsonl` | `sdk_stream` | assistant text or SDK stream message only |
| `tool_events.jsonl` | `hook_tool_event` | only source of tool started/completed/failed cards |
| `agent_messages.jsonl` | `agent_message` | assignment cards |
| `teammate_reports.jsonl` | `teammate_report` | report cards |
| `process_events.jsonl` | `process_event` | process state cards |
| `artifacts.jsonl` | `artifact` | artifact cards |
| `completion_checks.jsonl` | `completion_check` | completion status cards |
| `transitions.jsonl` | `runtime_snapshot` | lifecycle transitions |
| `trajectory.jsonl` | `runtime_snapshot` | trajectory steps and audit context |

The UI does not invent discussion text. If a run has tool events but no SDK text, agent message, or report text, the UI shows that only tool activity was captured.

## Transport

REST endpoints provide discovery, hydration, backfill, and raw-record lookup.

SSE endpoints provide live read-only updates:

```text
GET /api/repos/:repoKey/runs/:runId/stream?after=<seq>
GET /api/session-observer/stream?after=<seq>
```

The browser reconnects with `Last-Event-ID`; the explicit `after` query is also supported. Events are deduplicated by gateway `seq`.

## UI Structure

The prototype uses three panes:

- Repo / run / team tree
- Live activity stream
- Detail inspector with raw, trajectory, and model brief tabs

The team tree combines `session_bindings.jsonl`, `active_operations.json`, and hook tool events. A teammate running Bash is shown as running only when hook tool events or active operations say so. A report alone is displayed as a report, not as a fabricated tool action.

## Trajectory

When `trajectory.jsonl` exists, the trajectory tab shows:

- step index
- actor
- action
- observation
- state delta
- evidence refs
- raw refs

Selecting a completion, process, artifact, or tool event filters related trajectory steps by run/window/task IDs and evidence refs.

## Model Brief

The optional model brief endpoint is display-only.

Input:

- normalized events
- snapshot/status data
- unknowns

Output:

- explanatory copy
- unknowns

Forbidden:

- status decisions
- retry decisions
- route decisions
- completion decisions
- runtime writes
- agent instructions

If the brief fails, the main UI state remains unchanged.
